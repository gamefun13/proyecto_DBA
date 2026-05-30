"""
Carga archivos Microsoft Global ML Building Footprints (.geojson o .geojson.gz) → MongoDB
==========================================================================================

Base de datos : proyecto
Colección     : microsoft_buildings

Flujo:
    1. Lee archivos .geojson o .geojson.gz de Microsoft Buildings.
    2. Cruza cada edificio con municipios PDET.
    3. Inserta solo edificios dentro de municipios PDET.
    4. Crea índice geoespacial ANTES de insertar para que MongoDB rechace geometrías inválidas.
    5. Evita que el script se caiga al final por polígonos inválidos.

Ejecutar:
    py microsoftB.py
"""

import os
import glob
import gzip
import json
import time
import logging

import geopandas as gpd

from shapely.geometry import (
    shape,
    mapping,
    Polygon,
    MultiPolygon,
    GeometryCollection
)
from shapely.geometry.polygon import orient
from shapely.strtree import STRtree

from pymongo import MongoClient, GEOSPHERE
from pymongo.errors import BulkWriteError, OperationFailure

try:
    from shapely.validation import make_valid
except Exception:
    make_valid = None


# ============================================================
# CONFIGURACIÓN
# ============================================================

MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "proyecto"

COL_PDET = "territorios_pdet"
COL_BUILDINGS = "microsoft_buildings"

BATCH_SIZE = 20_000

# True = borra microsoft_buildings antes de volver a cargar
REINICIAR_COLECCION = True

# Si estás en carpeta microsoft_geojson, normalmente pdet_final.geojson está un nivel arriba
PDET_GEOJSON_PATHS = [
    "pdet_final.geojson",
    "../pdet_final.geojson",
    "../Recursos/pdet_final.geojson"
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# ============================================================
# UTILIDADES DE ARCHIVOS
# ============================================================

def abrir_archivo(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def cargar_geojson(path):
    with abrir_archivo(path) as f:
        return json.load(f)


# ============================================================
# UTILIDADES GENERALES
# ============================================================

def convertir_float_seguro(valor):
    if valor in (-1, None, ""):
        return None

    try:
        return float(valor)
    except Exception:
        return None


def convertir_str_seguro(valor):
    if valor is None:
        return None

    valor = str(valor).strip()

    if valor == "":
        return None

    return valor


def extraer_props(feature):
    props = feature.get("properties") or {}

    if isinstance(props, dict) and "properties" in props:
        props = props.get("properties") or {}

    height = props.get("height", None)
    confidence = props.get("confidence", None)

    return height, confidence


def obtener_propiedad(props, nombres):
    for nombre in nombres:
        if nombre in props:
            valor = props.get(nombre)

            if valor is not None and str(valor).strip() != "":
                return valor

    return None


# ============================================================
# LIMPIEZA GEOMÉTRICA
# ============================================================

def extraer_poligonos(geom):
    """
    Deja únicamente Polygon o MultiPolygon.
    Si make_valid devuelve GeometryCollection, extrae solo partes poligonales.
    """

    if geom is None or geom.is_empty:
        return None

    if isinstance(geom, Polygon):
        return geom

    if isinstance(geom, MultiPolygon):
        partes = [
            g for g in geom.geoms
            if isinstance(g, Polygon) and not g.is_empty
        ]

        if not partes:
            return None

        if len(partes) == 1:
            return partes[0]

        return MultiPolygon(partes)

    if isinstance(geom, GeometryCollection):
        partes = []

        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                partes.append(g)

            elif isinstance(g, MultiPolygon):
                partes.extend([
                    p for p in g.geoms
                    if isinstance(p, Polygon) and not p.is_empty
                ])

        if not partes:
            return None

        if len(partes) == 1:
            return partes[0]

        return MultiPolygon(partes)

    return None


def orientar_geometria(geom):
    """
    Orienta anillos de polígonos.
    """

    if geom is None or geom.is_empty:
        return None

    try:
        if isinstance(geom, Polygon):
            return orient(geom, sign=1.0)

        if isinstance(geom, MultiPolygon):
            partes = [
                orient(g, sign=1.0)
                for g in geom.geoms
                if not g.is_empty
            ]

            if not partes:
                return None

            if len(partes) == 1:
                return partes[0]

            return MultiPolygon(partes)

    except Exception:
        return None

    return None


def validar_geojson_mongo(geojson_geom):
    """
    Validación básica antes de insertar.
    MongoDB hará la validación geoespacial real gracias al índice 2dsphere.
    """

    if not isinstance(geojson_geom, dict):
        return False

    tipo = geojson_geom.get("type")
    coords = geojson_geom.get("coordinates")

    if tipo not in ["Polygon", "MultiPolygon"]:
        return False

    if not coords:
        return False

    try:
        if tipo == "Polygon":
            for ring in coords:
                if len(ring) < 4:
                    return False

                if ring[0] != ring[-1]:
                    return False

                for punto in ring:
                    if len(punto) < 2:
                        return False

                    lon = float(punto[0])
                    lat = float(punto[1])

                    if lon < -180 or lon > 180:
                        return False

                    if lat < -90 or lat > 90:
                        return False

        elif tipo == "MultiPolygon":
            for polygon in coords:
                for ring in polygon:
                    if len(ring) < 4:
                        return False

                    if ring[0] != ring[-1]:
                        return False

                    for punto in ring:
                        if len(punto) < 2:
                            return False

                        lon = float(punto[0])
                        lat = float(punto[1])

                        if lon < -180 or lon > 180:
                            return False

                        if lat < -90 or lat > 90:
                            return False

    except Exception:
        return False

    return True


def limpiar_geometria_para_mongo(geom_raw):
    """
    Convierte, repara y valida geometrías.

    Importante:
    Aunque Shapely diga que una geometría es válida, MongoDB puede rechazarla
    al indexarla como 2dsphere. Por eso el índice se crea ANTES de insertar.
    """

    if not geom_raw:
        return None, False

    try:
        geom_original = shape(geom_raw)
    except Exception:
        return None, False

    if geom_original is None or geom_original.is_empty:
        return None, False

    geom = extraer_poligonos(geom_original)

    if geom is None or geom.is_empty:
        return None, False

    reparada = False

    try:
        if make_valid is not None:
            geom_validada = make_valid(geom)

            if geom_validada is not None and not geom_validada.is_empty:
                if not geom.equals_exact(geom_validada, 0.0000001):
                    reparada = True

                geom = geom_validada

    except Exception:
        pass

    geom = extraer_poligonos(geom)

    if geom is None or geom.is_empty:
        return None, reparada

    try:
        geom_buffer = geom.buffer(0)

        if geom_buffer is not None and not geom_buffer.is_empty:
            if not geom.equals_exact(geom_buffer, 0.0000001):
                reparada = True

            geom = geom_buffer

    except Exception:
        return None, reparada

    geom = extraer_poligonos(geom)

    if geom is None or geom.is_empty:
        return None, reparada

    geom = orientar_geometria(geom)

    if geom is None or geom.is_empty:
        return None, reparada

    try:
        if not geom.is_valid:
            return None, reparada

        if geom.geom_type not in ["Polygon", "MultiPolygon"]:
            return None, reparada

    except Exception:
        return None, reparada

    geojson_geom = mapping(geom)

    if not validar_geojson_mongo(geojson_geom):
        return None, reparada

    return geom, reparada


# ============================================================
# MUNICIPIOS PDET
# ============================================================

def obtener_geometria_pdet_doc(doc):
    """
    Soporta varios nombres posibles de campo geométrico.
    """

    if doc.get("geometria_pdet"):
        return doc.get("geometria_pdet")

    if doc.get("geometry"):
        return doc.get("geometry")

    if doc.get("geometria"):
        return doc.get("geometria")

    if doc.get("geometria_municipio"):
        return doc.get("geometria_municipio")

    if doc.get("geom"):
        return doc.get("geom")

    return None


def construir_municipio_desde_props(props, geom):
    return {
        "codigo_mgn": convertir_str_seguro(
            obtener_propiedad(
                props,
                [
                    "codigo_mgn",
                    "CODIGO_MGN",
                    "COD_DANE_COMPLETO",
                    "Código DANE Municipio",
                    "Codigo DANE Municipio",
                    "cod_mpio",
                    "COD_MPIO",
                    "mpio_cdpmp",
                    "MPIO_CDPMP"
                ]
            )
        ),
        "nombre_municipio": convertir_str_seguro(
            obtener_propiedad(
                props,
                [
                    "nombre_municipio",
                    "NOMBRE_MUNICIPIO",
                    "municipio",
                    "Municipio",
                    "MPIO_CNMBR",
                    "mpio_cnmbr",
                    "NOM_MPIO",
                    "nom_mpio"
                ]
            )
        ),
        "departamento": convertir_str_seguro(
            obtener_propiedad(
                props,
                [
                    "departamento",
                    "Departamento",
                    "NOMBRE_DEPARTAMENTO",
                    "DPTO_CNMBR",
                    "dpto_cnmbr",
                    "NOM_DPTO",
                    "nom_dpto"
                ]
            )
        ),
        "subregion_pdet": convertir_str_seguro(
            obtener_propiedad(
                props,
                [
                    "subregion_pdet",
                    "Subregión PDET",
                    "Subregion PDET",
                    "SUBREGION",
                    "subregion",
                    "Subregion"
                ]
            )
        ),
        "geometry": geom
    }


def cargar_municipios_desde_mongo(db):
    log.info("Cargando municipios PDET desde MongoDB...")

    docs = list(db[COL_PDET].find({}))

    municipios = []

    for doc in docs:
        geom_json = obtener_geometria_pdet_doc(doc)

        if not geom_json:
            continue

        try:
            geom = shape(geom_json)
        except Exception:
            continue

        if geom is None or geom.is_empty:
            continue

        props = doc.get("properties", doc)

        municipio = construir_municipio_desde_props(props, geom)

        municipios.append(municipio)

    return municipios


def cargar_municipios_desde_geojson():
    for path in PDET_GEOJSON_PATHS:
        if not os.path.exists(path):
            continue

        log.warning(f"Cargando municipios PDET desde archivo local: {path}")

        with open(path, "r", encoding="utf-8") as f:
            geojson = json.load(f)

        features = geojson.get("features", [])

        municipios = []

        for feature in features:
            geom_json = feature.get("geometry")
            props = feature.get("properties", {}) or {}

            if not geom_json:
                continue

            try:
                geom = shape(geom_json)
            except Exception:
                continue

            if geom is None or geom.is_empty:
                continue

            municipio = construir_municipio_desde_props(props, geom)

            municipios.append(municipio)

        if municipios:
            return municipios

    return []


def cargar_municipios_pdet(db):
    municipios = cargar_municipios_desde_mongo(db)

    if not municipios:
        log.warning(
            f"No se pudieron leer municipios válidos desde '{COL_PDET}'. "
            "Intentando cargar desde pdet_final.geojson..."
        )

        municipios = cargar_municipios_desde_geojson()

    if not municipios:
        raise RuntimeError(
            f"No se pudieron cargar municipios PDET desde MongoDB ni desde {PDET_GEOJSON_PATHS}."
        )

    gdf = gpd.GeoDataFrame(municipios, crs="EPSG:4326")

    if len(gdf) == 0:
        raise RuntimeError("No se pudieron cargar geometrías PDET válidas.")

    log.info(f"Municipios PDET cargados: {len(gdf)}")

    return gdf


def preparar_indice_espacial(gdf_pdet):
    geoms = list(gdf_pdet.geometry)
    tree = STRtree(geoms)

    geom_id_to_index = {
        id(geom): i
        for i, geom in enumerate(geoms)
    }

    return geoms, tree, geom_id_to_index


def obtener_indices_candidatos(tree, geom_id_to_index, geometria_busqueda):
    try:
        candidatos = tree.query(geometria_busqueda)
    except Exception:
        return []

    indices = []

    for cand in candidatos:
        try:
            indices.append(int(cand))
            continue
        except Exception:
            pass

        idx = geom_id_to_index.get(id(cand))

        if idx is not None:
            indices.append(idx)

    return indices


def buscar_municipio_para_geom(geom, gdf_pdet, geoms, tree, geom_id_to_index):
    if geom is None or geom.is_empty:
        return None

    try:
        punto = geom.representative_point()
    except Exception:
        try:
            punto = geom.centroid
        except Exception:
            return None

    if punto is None or punto.is_empty:
        return None

    indices = obtener_indices_candidatos(tree, geom_id_to_index, punto)

    for idx in indices:
        try:
            poligono = geoms[idx]

            if poligono.covers(punto) or poligono.intersects(geom):
                fila = gdf_pdet.iloc[idx]

                return {
                    "codigo_mgn": fila["codigo_mgn"],
                    "nombre_municipio": fila["nombre_municipio"],
                    "departamento": fila.get("departamento"),
                    "subregion_pdet": fila.get("subregion_pdet"),
                }

        except Exception:
            continue

    indices = obtener_indices_candidatos(tree, geom_id_to_index, geom)

    for idx in indices:
        try:
            poligono = geoms[idx]

            if poligono.intersects(geom):
                fila = gdf_pdet.iloc[idx]

                return {
                    "codigo_mgn": fila["codigo_mgn"],
                    "nombre_municipio": fila["nombre_municipio"],
                    "departamento": fila.get("departamento"),
                    "subregion_pdet": fila.get("subregion_pdet"),
                }

        except Exception:
            continue

    return None


# ============================================================
# MONGODB
# ============================================================

def crear_indices(col):
    """
    Crea índices en colección vacía.
    Este punto es clave: el índice 2dsphere debe existir antes de insertar.
    """

    log.info("Creando índices...")

    col.create_index(
        [("geometria_edificio", GEOSPHERE)],
        name="idx_geo_edificio"
    )

    col.create_index(
        [("codigo_mgn", 1)],
        name="idx_codigo_mgn"
    )

    col.create_index(
        [("nombre_municipio", 1)],
        name="idx_nombre_municipio"
    )

    col.create_index(
        [("departamento", 1)],
        name="idx_departamento"
    )

    col.create_index(
        [("subregion_pdet", 1)],
        name="idx_subregion_pdet"
    )

    col.create_index(
        [("fuente", 1)],
        name="idx_fuente"
    )

    log.info("Índices creados correctamente.")


def preparar_coleccion(db):
    if REINICIAR_COLECCION:
        log.info(f"Eliminando colección '{COL_BUILDINGS}'...")
        db[COL_BUILDINGS].drop()

    col = db[COL_BUILDINGS]

    try:
        crear_indices(col)
    except OperationFailure as e:
        log.error(f"No se pudieron crear índices: {e}")
        raise

    return col


def insertar_lote(col, batch):
    """
    Inserta un lote.
    Si hay geometrías inválidas, MongoDB las rechaza por el índice 2dsphere,
    pero el resto del lote puede entrar.
    """

    if not batch:
        return 0, 0

    try:
        result = col.insert_many(batch, ordered=False)
        return len(result.inserted_ids), 0

    except BulkWriteError as e:
        detalles = e.details or {}

        insertados = detalles.get("nInserted", 0)
        errores = len(detalles.get("writeErrors", []))

        log.warning(
            f"Lote parcialmente insertado: "
            f"{insertados:,} insertados | {errores:,} rechazados por MongoDB"
        )

        return insertados, errores

    except Exception as e:
        log.warning(f"Lote rechazado completo: {e}")
        return 0, len(batch)


# ============================================================
# PROCESAMIENTO
# ============================================================

def procesar_archivo(path, col, gdf_pdet, geoms, tree, geom_id_to_index):
    nombre = os.path.basename(path)

    log.info(f"Procesando {nombre} ...")

    t0 = time.time()

    try:
        geojson = cargar_geojson(path)
    except Exception as e:
        log.warning(f"Error leyendo {nombre}: {e}")
        return 0, 0, 0, 0

    features = geojson.get("features", [])

    log.info(f"  {len(features):,} features en el archivo")

    batch = []

    insertados = 0
    omitidos = 0
    reparados = 0
    rechazados_mongo = 0
    procesados = 0

    for feature in features:
        procesados += 1

        try:
            geom_raw = feature.get("geometry")

            if not geom_raw:
                omitidos += 1
                continue

            geom, fue_reparada = limpiar_geometria_para_mongo(geom_raw)

            if geom is None:
                omitidos += 1
                continue

            if fue_reparada:
                reparados += 1

            municipio = buscar_municipio_para_geom(
                geom,
                gdf_pdet,
                geoms,
                tree,
                geom_id_to_index
            )

            if municipio is None:
                omitidos += 1
                continue

            height, confidence = extraer_props(feature)

            geojson_limpio = mapping(geom)

            if not validar_geojson_mongo(geojson_limpio):
                omitidos += 1
                continue

            doc = {
                "fuente": "microsoft",
                "codigo_mgn": municipio["codigo_mgn"],
                "nombre_municipio": municipio["nombre_municipio"],
                "departamento": municipio.get("departamento"),
                "subregion_pdet": municipio.get("subregion_pdet"),
                "geometria_edificio": geojson_limpio,
                "metadata": {
                    "altura_m": convertir_float_seguro(height),
                    "confianza": convertir_float_seguro(confidence),
                    "fuente_imagen": "Microsoft Bing Imagery",
                    "dataset_version": "Microsoft Global ML Building Footprints",
                },
            }

            batch.append(doc)

            if len(batch) >= BATCH_SIZE:
                nuevos, rechazados = insertar_lote(col, batch)

                insertados += nuevos
                rechazados_mongo += rechazados
                omitidos += rechazados

                batch = []

        except Exception:
            omitidos += 1
            continue

        if procesados % 100_000 == 0:
            tiempo = time.time() - t0

            log.info(
                f"  {nombre} | procesados: {procesados:,} | "
                f"insertados: {insertados:,} | "
                f"omitidos: {omitidos:,} | "
                f"reparados: {reparados:,} | "
                f"rechazados MongoDB: {rechazados_mongo:,} | "
                f"tiempo: {tiempo/60:.1f} min"
            )

    if batch:
        nuevos, rechazados = insertar_lote(col, batch)

        insertados += nuevos
        rechazados_mongo += rechazados
        omitidos += rechazados

    tiempo_total = time.time() - t0

    log.info(
        f"→ Terminado {nombre}: "
        f"{insertados:,} insertados | "
        f"{omitidos:,} omitidos | "
        f"{reparados:,} reparados | "
        f"{rechazados_mongo:,} rechazados MongoDB | "
        f"{tiempo_total/60:.1f} min"
    )

    return insertados, omitidos, reparados, rechazados_mongo


# ============================================================
# RESUMEN
# ============================================================

def mostrar_resumen(
    col,
    total_archivos,
    total_insertados,
    total_omitidos,
    total_reparados,
    total_rechazados_mongo,
    tiempo_total
):
    log.info("─── Resumen final ───────────────────────────────────────")
    log.info(f"Archivos procesados:            {total_archivos}")
    log.info(f"Total edificios insertados:     {total_insertados:,}")
    log.info(f"Total omitidos:                 {total_omitidos:,}")
    log.info(f"Total reparados:                {total_reparados:,}")
    log.info(f"Total rechazados por MongoDB:   {total_rechazados_mongo:,}")
    log.info(f"Total en colección:             {col.count_documents({}):,}")
    log.info(f"Tiempo total:                   {tiempo_total/60:.1f} min")

    log.info("Top 10 municipios:")

    pipeline = [
        {
            "$group": {
                "_id": "$nombre_municipio",
                "total": {"$sum": 1}
            }
        },
        {
            "$sort": {
                "total": -1
            }
        },
        {
            "$limit": 10
        }
    ]

    for r in col.aggregate(pipeline):
        nombre = r.get("_id") or "SIN MUNICIPIO"
        total = r.get("total", 0)
        log.info(f"{total:10,}  {nombre}")

    log.info("─────────────────────────────────────────────────────────")


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=== SCRIPT: Carga Microsoft Buildings → MongoDB ===")

    archivos = (
        glob.glob("*.geojson.gz") +
        glob.glob("*.geojson")
    )

    # Evita procesar pdet_final.geojson si por accidente está en esta carpeta
    archivos = [
        a for a in archivos
        if os.path.basename(a).lower() != "pdet_final.geojson"
    ]

    archivos = sorted(list(set(archivos)))

    if not archivos:
        log.error("No se encontraron archivos .geojson / .geojson.gz de Microsoft.")
        return

    log.info(f"Archivos encontrados: {len(archivos)}")

    for archivo in archivos:
        log.info(f"  {archivo}")

    cliente = MongoClient(MONGO_URI)
    db = cliente[DB_NAME]

    gdf_pdet = cargar_municipios_pdet(db)

    geoms, tree, geom_id_to_index = preparar_indice_espacial(gdf_pdet)

    col = preparar_coleccion(db)

    total_insertados = 0
    total_omitidos = 0
    total_reparados = 0
    total_rechazados_mongo = 0

    t_global = time.time()

    for archivo in archivos:
        insertados, omitidos, reparados, rechazados_mongo = procesar_archivo(
            archivo,
            col,
            gdf_pdet,
            geoms,
            tree,
            geom_id_to_index
        )

        total_insertados += insertados
        total_omitidos += omitidos
        total_reparados += reparados
        total_rechazados_mongo += rechazados_mongo

        log.info(
            f"Acumulado global: "
            f"{total_insertados:,} insertados | "
            f"{total_omitidos:,} omitidos | "
            f"{total_reparados:,} reparados | "
            f"{total_rechazados_mongo:,} rechazados MongoDB"
        )

    tiempo_total = time.time() - t_global

    mostrar_resumen(
        col,
        total_archivos=len(archivos),
        total_insertados=total_insertados,
        total_omitidos=total_omitidos,
        total_reparados=total_reparados,
        total_rechazados_mongo=total_rechazados_mongo,
        tiempo_total=tiempo_total
    )

    cliente.close()

    log.info("=== Script completado ✓ ===")


if __name__ == "__main__":
    main()