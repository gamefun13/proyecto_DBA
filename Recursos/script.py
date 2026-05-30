"""
Carga archivos Google Open Buildings (.csv o .csv.gz) → MongoDB
================================================================

Base de datos : proyecto
Colección     : google_buildings

Requisitos:
    1. Tener MongoDB corriendo.
    2. Tener cargada la colección territorios_pdet
       o tener el archivo pdet_final.geojson en la misma carpeta.
    3. Tener archivos Google tipo:
            *_buildings.csv
            *_buildings.csv.gz
    4. Ejecutar:
            py script.py
"""

import os
import glob
import gzip
import json
import logging
from datetime import datetime, timezone

import pandas as pd
from shapely.geometry import shape, mapping
from shapely.wkt import loads as wkt_loads
from shapely.strtree import STRtree

from pymongo import MongoClient, GEOSPHERE, InsertOne
from pymongo.errors import BulkWriteError


# ============================================================
# CONFIGURACIÓN
# ============================================================

MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "proyecto"

COL_PDET = "territorios_pdet"
COLLECTION = "google_buildings"

PDET_GEOJSON_PATH = "pdet_final.geojson"

MIN_CONF = 0.6
BATCH_SIZE = 2000
CHUNK_SIZE = 50000

BORRAR_COLECCION_ANTES = True


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# ============================================================
# UTILIDADES
# ============================================================

def abrir_csv_chunks(path, chunksize=CHUNK_SIZE):
    """
    Lee CSV o CSV.GZ por bloques para evitar cargar todo en RAM.
    """

    if path.endswith(".gz"):
        return pd.read_csv(
            path,
            compression="gzip",
            chunksize=chunksize
        )

    return pd.read_csv(
        path,
        chunksize=chunksize
    )


def valor_str(valor):
    """
    Convierte valores a string limpio.
    """

    if valor is None:
        return None

    valor = str(valor).strip()

    if valor == "":
        return None

    return valor


def valor_float(valor):
    """
    Convierte valores a float de forma segura.
    """

    try:
        if valor is None:
            return None

        if pd.isna(valor):
            return None

        return float(valor)

    except Exception:
        return None


def reparar_geometria(geom):
    """
    Intenta reparar geometrías inválidas.
    """

    try:
        if geom is None or geom.is_empty:
            return None

        if not geom.is_valid:
            geom = geom.buffer(0)

        if geom is None or geom.is_empty or not geom.is_valid:
            return None

        return geom

    except Exception:
        return None


def obtener_propiedad(props, nombres):
    """
    Busca una propiedad usando varios nombres posibles.
    """

    for nombre in nombres:
        if nombre in props:
            valor = props.get(nombre)

            if valor is not None and str(valor).strip() != "":
                return valor

    return None


def detectar_columna_geometry(columnas):
    """
    Detecta la columna de geometría WKT del CSV.
    """

    posibles = [
        "geometry",
        "Geometry",
        "GEOMETRY",
        "wkt",
        "WKT"
    ]

    for nombre in posibles:
        if nombre in columnas:
            return nombre

    return None


def obtener_geometria_pdet_doc(doc):
    """
    Obtiene la geometría de un documento PDET aunque tenga distintos nombres.
    """

    if doc.get("geometry"):
        return doc.get("geometry")

    if doc.get("geometria_pdet"):
        return doc.get("geometria_pdet")

    if doc.get("geometria"):
        return doc.get("geometria")

    if doc.get("geometria_municipio"):
        return doc.get("geometria_municipio")

    if doc.get("geom"):
        return doc.get("geom")

    return None


# ============================================================
# MUNICIPIOS PDET
# ============================================================

def cargar_municipios_desde_mongo(db):
    """
    Carga municipios PDET desde MongoDB.
    """

    log.info("Cargando municipios PDET desde MongoDB...")

    docs = list(db[COL_PDET].find({}))

    municipios = []
    geoms = []

    for doc in docs:
        geom_data = obtener_geometria_pdet_doc(doc)

        if not geom_data:
            continue

        try:
            geom = shape(geom_data)
            geom = reparar_geometria(geom)

            if geom is None:
                continue

            props = doc.get("properties", doc)

            municipio = {
                "codigo_mgn": valor_str(
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
                "nombre_municipio": valor_str(
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
                "departamento": valor_str(
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
                "subregion_pdet": valor_str(
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

            municipios.append(municipio)
            geoms.append(geom)

        except Exception:
            continue

    return municipios, geoms


def cargar_municipios_desde_geojson(path):
    """
    Carga municipios PDET desde pdet_final.geojson.
    """

    log.warning(f"Intentando cargar municipios PDET desde archivo local: {path}")

    if not os.path.exists(path):
        return [], []

    with open(path, "r", encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    municipios = []
    geoms = []

    for feature in features:
        props = feature.get("properties", {}) or {}
        geom_data = feature.get("geometry")

        if not geom_data:
            continue

        try:
            geom = shape(geom_data)
            geom = reparar_geometria(geom)

            if geom is None:
                continue

            municipio = {
                "codigo_mgn": valor_str(
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
                "nombre_municipio": valor_str(
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
                "departamento": valor_str(
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
                "subregion_pdet": valor_str(
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

            municipios.append(municipio)
            geoms.append(geom)

        except Exception:
            continue

    return municipios, geoms


def cargar_municipios_pdet(db):
    """
    Carga municipios PDET desde MongoDB.
    Si falla, carga desde pdet_final.geojson.
    """

    municipios, geoms = cargar_municipios_desde_mongo(db)

    if not municipios:
        log.warning(
            f"No se pudieron leer municipios válidos desde '{COL_PDET}'."
        )
        municipios, geoms = cargar_municipios_desde_geojson(PDET_GEOJSON_PATH)

    if not municipios:
        raise RuntimeError(
            "No se pudieron cargar municipios PDET ni desde MongoDB "
            f"ni desde '{PDET_GEOJSON_PATH}'."
        )

    tree = STRtree(geoms)

    geom_id_to_index = {
        id(geom): idx
        for idx, geom in enumerate(geoms)
    }

    log.info(f"Municipios PDET cargados: {len(municipios)}")

    return municipios, geoms, tree, geom_id_to_index


def obtener_indices_candidatos(candidatos, geom_id_to_index):
    """
    Compatible con Shapely 1.x y Shapely 2.x.
    Shapely 2.x devuelve índices.
    Shapely 1.x devuelve geometrías.
    """

    indices = []

    for candidato in candidatos:
        try:
            indices.append(int(candidato))
            continue
        except Exception:
            pass

        idx = geom_id_to_index.get(id(candidato))

        if idx is not None:
            indices.append(idx)

    return indices


def buscar_municipio_para_geom(geom_edificio, municipios, geoms, tree, geom_id_to_index):
    """
    Busca el municipio PDET que contiene el centroide del edificio.
    """

    try:
        punto = geom_edificio.centroid
    except Exception:
        return None

    try:
        candidatos = tree.query(punto)
    except Exception:
        return None

    indices = obtener_indices_candidatos(candidatos, geom_id_to_index)

    for idx in indices:
        try:
            geom_mpio = geoms[idx]

            if geom_mpio.contains(punto) or geom_mpio.intersects(geom_edificio):
                return municipios[idx]

        except Exception:
            continue

    return None


# ============================================================
# MONGODB
# ============================================================

def preparar_coleccion(col):
    """
    Borra colección si aplica y crea índices.
    """

    if BORRAR_COLECCION_ANTES:
        log.info(f"Eliminando colección '{COLLECTION}' antes de cargar...")
        col.drop()

    log.info("Creando índices...")

    col.create_index(
        [("geometria_edificio", GEOSPHERE)],
        name="idx_geo_edificio"
    )

    col.create_index(
        [("fuente", 1)],
        name="idx_fuente"
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
        [("metadata.confianza", 1)],
        name="idx_confianza"
    )

    log.info("Índices creados.")


def insertar_batch(col, batch):
    """
    Inserta lote en MongoDB.
    """

    if not batch:
        return 0

    try:
        result = col.bulk_write(batch, ordered=False)
        return result.inserted_count

    except BulkWriteError as e:
        inserted = e.details.get("nInserted", 0)
        errores = len(e.details.get("writeErrors", []))

        log.warning(
            f"BulkWriteError: {inserted} insertados, {errores} errores."
        )

        return inserted

    except Exception as e:
        log.error(f"Error insertando batch: {e}")
        return 0


# ============================================================
# PROCESAMIENTO GOOGLE BUILDINGS
# ============================================================

def procesar_chunk(
    df,
    archivo,
    col,
    municipios,
    geoms,
    tree,
    geom_id_to_index
):
    """
    Procesa un bloque del CSV.
    """

    columnas = list(df.columns)

    col_geometry = detectar_columna_geometry(columnas)

    if not col_geometry:
        raise RuntimeError(
            f"El archivo {archivo} no tiene columna geometry/WKT."
        )

    if "confidence" not in columnas:
        raise RuntimeError(
            f"El archivo {archivo} no tiene columna confidence."
        )

    df = df[df["confidence"] >= MIN_CONF].copy()

    batch = []

    procesados = 0
    insertados = 0
    omitidos = 0
    sin_municipio = 0

    for _, row in df.iterrows():
        procesados += 1

        try:
            geom_wkt = row.get(col_geometry)

            if geom_wkt is None or pd.isna(geom_wkt):
                omitidos += 1
                continue

            geom = wkt_loads(str(geom_wkt))
            geom = reparar_geometria(geom)

            if geom is None:
                omitidos += 1
                continue

            municipio = buscar_municipio_para_geom(
                geom,
                municipios,
                geoms,
                tree,
                geom_id_to_index
            )

            if municipio is None:
                sin_municipio += 1
                omitidos += 1
                continue

            area = valor_float(row.get("area_in_meters"))
            confianza = valor_float(row.get("confidence"))

            doc = {
                "fuente": "google",
                "codigo_mgn": municipio.get("codigo_mgn"),
                "nombre_municipio": municipio.get("nombre_municipio"),
                "departamento": municipio.get("departamento"),
                "subregion_pdet": municipio.get("subregion_pdet"),
                "geometria_edificio": mapping(geom),
                "area_estimada_m2": round(area, 2) if area is not None else None,
                "metadata": {
                    "archivo_origen": os.path.basename(archivo),
                    "confianza": round(confianza, 4) if confianza is not None else None,
                    "latitude": valor_float(row.get("latitude")),
                    "longitude": valor_float(row.get("longitude")),
                    "full_plus_code": valor_str(row.get("full_plus_code")),
                    "fuente_imagen": "Google Satellite Imagery",
                    "dataset_version": "Google Open Buildings",
                    "fecha_carga": datetime.now(timezone.utc)
                }
            }

            batch.append(InsertOne(doc))

            if len(batch) >= BATCH_SIZE:
                nuevos = insertar_batch(col, batch)
                insertados += nuevos
                batch = []

        except Exception:
            omitidos += 1
            continue

    if batch:
        nuevos = insertar_batch(col, batch)
        insertados += nuevos

    return procesados, insertados, omitidos, sin_municipio


def procesar_archivo(
    archivo,
    col,
    municipios,
    geoms,
    tree,
    geom_id_to_index
):
    """
    Procesa un archivo CSV completo por chunks.
    """

    log.info(f"Procesando {archivo} ...")

    total_procesados = 0
    total_insertados = 0
    total_omitidos = 0
    total_sin_municipio = 0

    try:
        chunks = abrir_csv_chunks(archivo)

        for num_chunk, df in enumerate(chunks, start=1):
            procesados, insertados, omitidos, sin_municipio = procesar_chunk(
                df,
                archivo,
                col,
                municipios,
                geoms,
                tree,
                geom_id_to_index
            )

            total_procesados += procesados
            total_insertados += insertados
            total_omitidos += omitidos
            total_sin_municipio += sin_municipio

            log.info(
                f"  {os.path.basename(archivo)} | chunk {num_chunk} | "
                f"procesados: {total_procesados:,} | "
                f"insertados: {total_insertados:,} | "
                f"omitidos: {total_omitidos:,} | "
                f"sin municipio PDET: {total_sin_municipio:,}"
            )

    except Exception as e:
        log.error(f"Error procesando {archivo}: {e}")

    log.info(
        f"→ Terminado {os.path.basename(archivo)}: "
        f"{total_insertados:,} insertados | "
        f"{total_omitidos:,} omitidos | "
        f"{total_sin_municipio:,} sin municipio PDET"
    )

    return total_insertados, total_omitidos, total_sin_municipio


# ============================================================
# RESUMEN
# ============================================================

def mostrar_resumen(col, total_insertados, total_omitidos, total_sin_municipio):
    """
    Muestra resumen final.
    """

    log.info("─── Resumen ─────────────────────────────────────────────")
    log.info(f"Total edificios insertados: {total_insertados:,}")
    log.info(f"Total omitidos:             {total_omitidos:,}")
    log.info(f"Total sin municipio PDET:   {total_sin_municipio:,}")
    log.info(f"Total en colección:         {col.count_documents({}):,}")

    log.info("Top 10 municipios:")

    pipeline = [
        {
            "$group": {
                "_id": "$nombre_municipio",
                "n": {"$sum": 1}
            }
        },
        {
            "$sort": {
                "n": -1
            }
        },
        {
            "$limit": 10
        }
    ]

    for r in col.aggregate(pipeline):
        nombre = r.get("_id") or "SIN MUNICIPIO"
        total = r.get("n", 0)
        log.info(f"{total:10,}  {nombre}")

    log.info("─────────────────────────────────────────────────────────")


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=== Cargando Google Buildings → MongoDB ===")

    archivos = (
        glob.glob("*_buildings*.csv.gz") +
        glob.glob("*_buildings*.csv")
    )

    archivos = sorted(set(archivos))

    if not archivos:
        log.error("No se encontraron archivos *_buildings*.csv en esta carpeta.")
        return

    log.info(f"Archivos encontrados: {len(archivos)}")

    for archivo in archivos:
        log.info(f"  {archivo}")

    cliente = MongoClient(MONGO_URI)
    db = cliente[DB_NAME]

    municipios, geoms, tree, geom_id_to_index = cargar_municipios_pdet(db)

    col = db[COLLECTION]

    preparar_coleccion(col)

    total_insertados = 0
    total_omitidos = 0
    total_sin_municipio = 0

    for archivo in archivos:
        insertados, omitidos, sin_municipio = procesar_archivo(
            archivo,
            col,
            municipios,
            geoms,
            tree,
            geom_id_to_index
        )

        total_insertados += insertados
        total_omitidos += omitidos
        total_sin_municipio += sin_municipio

        log.info(
            f"Acumulado global: "
            f"{total_insertados:,} insertados | "
            f"{total_omitidos:,} omitidos | "
            f"{total_sin_municipio:,} sin municipio PDET"
        )

    mostrar_resumen(
        col,
        total_insertados,
        total_omitidos,
        total_sin_municipio
    )

    cliente.close()

    log.info("Listo.")


if __name__ == "__main__":
    main()