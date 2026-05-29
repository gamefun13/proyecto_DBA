"""
Carga archivos Google Open Buildings (.csv o .csv.gz) → MongoDB
================================================================
Base de datos : proyecto
Colección     : google_buildings

Requisitos:
    1. Tener MongoDB corriendo.
    2. Haber cargado primero la colección territorios_pdet.
    3. Poner los archivos *_buildings*.csv o *_buildings*.csv.gz en esta misma carpeta.
    4. Ejecutar: python 02_cargar_google_buildings.py
"""

import os
import glob
import gzip
import json
import time
import re
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
from pymongo.errors import OperationFailure

try:
    from shapely.validation import make_valid
except Exception:
    make_valid = None


# =========================
# CONFIGURACIÓN
# =========================
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "proyecto"

COL_PDET = "territorios_pdet"
COL_BUILDINGS = "microsoft_buildings"

BATCH_SIZE = 20_000
REINICIAR_COLECCION = True

# Si MongoDB encuentra geometrías inválidas al crear el índice 2dsphere,
# las elimina automáticamente y vuelve a intentar.
AUTO_LIMPIAR_INVALIDOS_INDICE = True
MAX_INTENTOS_INDICE_GEO = 500


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# =========================
# ARCHIVOS
# =========================
def abrir_archivo(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def cargar_geojson(path):
    with abrir_archivo(path) as f:
        return json.load(f)


# =========================
# UTILIDADES
# =========================
def convertir_float_seguro(valor):
    if valor in (-1, None, ""):
        return None

    try:
        return float(valor)
    except Exception:
        return None


def extraer_props(feature):
    props = feature.get("properties") or {}

    if isinstance(props, dict) and "properties" in props:
        props = props.get("properties") or {}

    height = props.get("height", None)
    confidence = props.get("confidence", None)

    return height, confidence


# =========================
# LIMPIEZA GEOMÉTRICA
# =========================
def extraer_poligonos(geom):
    """
    Devuelve únicamente Polygon o MultiPolygon.
    Si llega GeometryCollection, extrae solo las partes poligonales.
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
    Ayuda a que MongoDB acepte mejor el GeoJSON para índice 2dsphere.
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
    No detecta todos los errores topológicos de MongoDB,
    pero descarta geometrías claramente problemáticas.
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

                    if lon < -180 or lon > 180 or lat < -90 or lat > 90:
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

                        if lon < -180 or lon > 180 or lat < -90 or lat > 90:
                            return False

    except Exception:
        return False

    return True


def limpiar_geometria_para_mongo(geom_raw):
    """
    Convierte, repara y valida geometrías antes de insertarlas.
    Reduce errores como:
    - Can't extract geo keys
    - Loop is not valid
    - Edges cross
    """

    if not geom_raw:
        return None

    try:
        geom = shape(geom_raw)
    except Exception:
        return None

    if geom is None or geom.is_empty:
        return None

    geom = extraer_poligonos(geom)

    if geom is None or geom.is_empty:
        return None

    # Reparación 1: make_valid si está disponible
    try:
        if make_valid is not None:
            geom = make_valid(geom)
    except Exception:
        pass

    geom = extraer_poligonos(geom)

    if geom is None or geom.is_empty:
        return None

    # Reparación 2: buffer(0)
    try:
        geom = geom.buffer(0)
    except Exception:
        return None

    geom = extraer_poligonos(geom)

    if geom is None or geom.is_empty:
        return None

    # Reparación 3: orientar anillos
    geom = orientar_geometria(geom)

    if geom is None or geom.is_empty:
        return None

    if not geom.is_valid:
        return None

    if geom.geom_type not in ["Polygon", "MultiPolygon"]:
        return None

    geojson_geom = mapping(geom)

    if not validar_geojson_mongo(geojson_geom):
        return None

    return geom


# =========================
# MUNICIPIOS PDET
# =========================
def cargar_municipios_pdet(db):
    docs = list(
        db[COL_PDET].find(
            {},
            {
                "codigo_mgn": 1,
                "nombre_municipio": 1,
                "departamento": 1,
                "subregion_pdet": 1,
                "geometria_pdet": 1,
            }
        )
    )

    if not docs:
        raise RuntimeError(
            f"No hay documentos en '{COL_PDET}'. Ejecuta primero el script de municipios PDET."
        )

    municipios = []

    for d in docs:
        geom_json = d.get("geometria_pdet")

        if not geom_json:
            continue

        try:
            geom = shape(geom_json)
        except Exception:
            continue

        if geom is None or geom.is_empty:
            continue

        municipios.append(
            {
                "codigo_mgn": d.get("codigo_mgn"),
                "nombre_municipio": d.get("nombre_municipio"),
                "departamento": d.get("departamento"),
                "subregion_pdet": d.get("subregion_pdet"),
                "geometry": geom,
            }
        )

    gdf = gpd.GeoDataFrame(municipios, crs="EPSG:4326")

    if len(gdf) == 0:
        raise RuntimeError("No se pudieron cargar geometrías PDET válidas.")

    return gdf


def preparar_indice_espacial(gdf_pdet):
    geoms = list(gdf_pdet.geometry)
    tree = STRtree(geoms)

    # Compatibilidad Shapely 1.x
    geom_id_to_index = {id(geom): i for i, geom in enumerate(geoms)}

    return geoms, tree, geom_id_to_index


def obtener_indices_candidatos(tree, geom_id_to_index, punto):
    candidatos = tree.query(punto)
    indices = []

    for cand in candidatos:
        # Shapely 2.x devuelve índices
        try:
            indices.append(int(cand))
            continue
        except Exception:
            pass

        # Shapely 1.x devuelve geometrías
        idx = geom_id_to_index.get(id(cand))

        if idx is not None:
            indices.append(idx)

    return indices


def buscar_municipio_para_geom(geom, gdf_pdet, geoms, tree, geom_id_to_index):
    if geom is None or geom.is_empty:
        return None

    punto = geom.centroid

    if punto is None or punto.is_empty:
        return None

    indices = obtener_indices_candidatos(tree, geom_id_to_index, punto)

    for idx in indices:
        poligono = geoms[idx]

        if poligono.covers(punto):
            fila = gdf_pdet.iloc[idx]

            return {
                "codigo_mgn": fila["codigo_mgn"],
                "nombre_municipio": fila["nombre_municipio"],
                "departamento": fila.get("departamento"),
                "subregion_pdet": fila.get("subregion_pdet"),
            }

    return None


# =========================
# MONGODB
# =========================
def insertar_lote(col, batch):
    if batch:
        col.insert_many(batch, ordered=False)


def crear_indice_geo_con_limpieza(col):
    """
    Crea el índice 2dsphere.
    Si MongoDB encuentra documentos con geometrías inválidas,
    extrae el ObjectId del error, elimina el documento y reintenta.
    """

    log.info("Creando índice geoespacial 2dsphere...")

    for intento in range(1, MAX_INTENTOS_INDICE_GEO + 1):
        try:
            col.create_index(
                [("geometria_edificio", GEOSPHERE)],
                name="idx_geo_edificio"
            )

            log.info(
                f"✅ Índice geoespacial creado correctamente. Intentos: {intento}"
            )
            return

        except OperationFailure as e:
            msg = str(e)

            match = re.search(r"ObjectId\('([a-fA-F0-9]+)'\)", msg)

            if not match:
                log.error("No se pudo extraer ObjectId del error de índice.")
                log.error(msg)
                raise

            bad_id = match.group(1)

            log.warning(
                f"Documento con geometría inválida encontrado. "
                f"Eliminando _id={bad_id}"
            )

            res = col.delete_one({"_id": __import__("bson").ObjectId(bad_id)})

            log.warning(f"Documento eliminado: {res.deleted_count}")

    raise RuntimeError(
        f"No se pudo crear el índice geoespacial después de "
        f"{MAX_INTENTOS_INDICE_GEO} intentos."
    )


def crear_indices(col):
    log.info("Creando índices finales...")

    if AUTO_LIMPIAR_INVALIDOS_INDICE:
        crear_indice_geo_con_limpieza(col)
    else:
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


# =========================
# PROCESAMIENTO
# =========================
def procesar_archivo(path, col, gdf_pdet, geoms, tree, geom_id_to_index):
    nombre = os.path.basename(path)

    log.info(f"Procesando {nombre} ...")

    t0 = time.time()

    try:
        geojson = cargar_geojson(path)
    except Exception as e:
        log.warning(f"Error leyendo {nombre}: {e}")
        return 0, 0, 0

    features = geojson.get("features", [])

    log.info(f"  {len(features):,} features en el archivo")

    batch = []
    insertados = 0
    omitidos = 0
    reparados = 0
    procesados = 0

    for feature in features:
        procesados += 1

        try:
            geom_raw = feature.get("geometry")

            if not geom_raw:
                omitidos += 1
                continue

            try:
                geom_original = shape(geom_raw)
            except Exception:
                omitidos += 1
                continue

            geom = limpiar_geometria_para_mongo(geom_raw)

            if geom is None:
                omitidos += 1
                continue

            try:
                if (not geom_original.is_valid) or (
                    not geom_original.equals_exact(geom, 0.0000001)
                ):
                    reparados += 1
            except Exception:
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
                insertar_lote(col, batch)
                insertados += len(batch)
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
                f"tiempo: {tiempo/60:.1f} min"
            )

    if batch:
        insertar_lote(col, batch)
        insertados += len(batch)

    tiempo_total = time.time() - t0

    log.info(
        f"→ Terminado {nombre}: "
        f"{insertados:,} insertados | "
        f"{omitidos:,} omitidos | "
        f"{reparados:,} reparados | "
        f"{tiempo_total/60:.1f} min"
    )

    return insertados, omitidos, reparados


# =========================
# MAIN
# =========================
def main():
    log.info("=== SCRIPT 3: Carga Microsoft Buildings → MongoDB ===")

    archivos = (
        glob.glob("*.geojson.gz") +
        glob.glob("*.geojson") +
        glob.glob("*.json")
    )

    archivos = sorted(list(set(archivos)))

    if not archivos:
        log.error("No se encontraron archivos .geojson / .geojson.gz / .json.")
        return

    log.info(f"Archivos encontrados: {len(archivos)}")

    for archivo in archivos:
        log.info(f"  {archivo}")

    cliente = MongoClient(MONGO_URI)
    db = cliente[DB_NAME]

    log.info("Cargando municipios PDET desde MongoDB...")
    gdf_pdet = cargar_municipios_pdet(db)

    log.info(f"Municipios PDET cargados: {len(gdf_pdet)}")

    geoms, tree, geom_id_to_index = preparar_indice_espacial(gdf_pdet)

    if REINICIAR_COLECCION:
        log.info(f"Eliminando colección '{COL_BUILDINGS}'...")
        db[COL_BUILDINGS].drop()

    col = db[COL_BUILDINGS]

    total_insertados = 0
    total_omitidos = 0
    total_reparados = 0

    t_global = time.time()

    for archivo in archivos:
        insertados, omitidos, reparados = procesar_archivo(
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

        log.info(
            f"Acumulado global: "
            f"{total_insertados:,} insertados | "
            f"{total_omitidos:,} omitidos | "
            f"{total_reparados:,} reparados"
        )

    crear_indices(col)

    log.info("─── Resumen final ───────────────────────────────────────")
    log.info(f"Archivos procesados:        {len(archivos)}")
    log.info(f"Total edificios insertados: {total_insertados:,}")
    log.info(f"Total omitidos:             {total_omitidos:,}")
    log.info(f"Total reparados:            {total_reparados:,}")
    log.info(f"Total en colección:         {col.count_documents({}):,}")
    log.info(f"Tiempo total:               {(time.time() - t_global)/60:.1f} min")

    log.info("Top 10 municipios:")

    pipeline = [
        {
            "$group": {
                "_id": "$nombre_municipio",
                "total": {"$sum": 1}
            }
        },
        {"$sort": {"total": -1}},
        {"$limit": 10}
    ]

    for r in col.aggregate(pipeline):
        log.info(f"  {r['total']:>8,}  {r['_id']}")

    log.info("─────────────────────────────────────────────────────────")

    cliente.close()
    log.info("=== Script 3 completado ✓ ===")


if __name__ == "__main__":
    main()