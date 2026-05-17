"""
Carga archivos Google Open Buildings (.csv o .csv.gz) → MongoDB
================================================================
Base de datos : proyecto
Colección     : google_buildings

Instrucciones:
    1. Poner los archivos *_buildings*.csv en la misma carpeta
    2. Tener MongoDB corriendo
    3. Ejecutar: python cargar_csv.py
"""

import os
import glob
import gzip
import logging
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape, mapping
from shapely.wkt import loads as wkt_loads
from shapely.strtree import STRtree
from pymongo import MongoClient, GEOSPHERE, InsertOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MONGO_URI  = "mongodb://localhost:27017/"
DB_NAME    = "proyecto"
COLLECTION = "google_buildings"
MIN_CONF   = 0.6
BATCH_SIZE = 2000


def leer_archivo(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return pd.read_csv(f)
    else:
        return pd.read_csv(path)


def main():
    log.info("=== Cargando Google Buildings → MongoDB (proyecto) ===")

    archivos = glob.glob("*_buildings*.csv.gz") + glob.glob("*_buildings*.csv")
    archivos = list(set(archivos))

    if not archivos:
        log.error("No se encontraron archivos *_buildings*.csv en esta carpeta.")
        return

    log.info(f"Archivos encontrados: {len(archivos)}")
    for a in archivos:
        log.info(f"  {a}")

    cliente = MongoClient(MONGO_URI)
    db      = cliente[DB_NAME]
    docs    = list(db["territorios_pdet"].find(
        {}, {"codigo_mgn": 1, "nombre_municipio": 1, "geometria_pdet": 1}
    ))

    if not docs:
        log.error("No hay municipios en 'territorios_pdet'. Corre primero el script de PDET.")
        cliente.close()
        return

    features = []
    for d in docs:
        geom = shape(d["geometria_pdet"])
        features.append({
            "codigo_mgn":       d["codigo_mgn"],
            "nombre_municipio": d["nombre_municipio"],
            "geometry":         geom,
        })

    gdf   = gpd.GeoDataFrame(features, crs="EPSG:4326")
    geoms = list(gdf.geometry)
    tree  = STRtree(geoms)
    log.info(f"Municipios PDET cargados: {len(gdf)}")

    col     = db[COLLECTION]
    indices = [i["name"] for i in col.list_indexes()]
    if "idx_geo_edificio" not in indices:
        col.create_index([("geometria_edificio", GEOSPHERE)], name="idx_geo_edificio")
        col.create_index("fuente",     name="idx_fuente")
        col.create_index("codigo_mgn", name="idx_codigo_mgn")
        log.info("Índices creados.")

    total_global = 0

    for archivo in archivos:
        log.info(f"Procesando {archivo} ...")
        try:
            df = leer_archivo(archivo)
        except Exception as e:
            log.warning(f"Error leyendo {archivo}: {e}")
            continue

        df = df[df["confidence"] >= MIN_CONF].copy()
        log.info(f"  {len(df):,} edificios con confianza >= {MIN_CONF}")

        batch = []
        count = 0

        for _, row in df.iterrows():
            try:
                geom = wkt_loads(row["geometry"])
                if geom is None or geom.is_empty:
                    continue

                candidatos      = tree.query(geom)
                municipio_match = None
                for idx in candidatos:
                    if geoms[idx].contains(geom.centroid):
                        fila = gdf.iloc[idx]
                        municipio_match = {
                            "codigo_mgn":       fila["codigo_mgn"],
                            "nombre_municipio": fila["nombre_municipio"],
                        }
                        break

                if municipio_match is None:
                    continue

                doc = {
                    "fuente":             "google",
                    "codigo_mgn":         municipio_match["codigo_mgn"],
                    "nombre_municipio":   municipio_match["nombre_municipio"],
                    "geometria_edificio": mapping(geom),
                    "area_estimada_m2":   round(float(row["area_in_meters"]), 2),
                    "metadata": {
                        "confianza":       round(float(row["confidence"]), 4),
                        "full_plus_code":  str(row.get("full_plus_code", "")),
                        "fuente_imagen":   "Google Satellite Imagery",
                        "dataset_version": "Google Open Buildings v3",
                    },
                }
                batch.append(InsertOne(doc))
                count += 1

                if len(batch) >= BATCH_SIZE:
                    col.bulk_write(batch, ordered=False)
                    batch = []

            except Exception:
                continue

        if batch:
            col.bulk_write(batch, ordered=False)

        log.info(f"  → {count:,} edificios PDET insertados de {os.path.basename(archivo)}")
        total_global += count

    log.info("─── Resumen ─────────────────────────────────────────────")
    log.info(f"  Total edificios insertados: {total_global:,}")
    log.info(f"  Total en coleccion:         {col.count_documents({}):,}")
    log.info("  Top 10 municipios:")
    for r in col.aggregate([
        {"$group": {"_id": "$nombre_municipio", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 10}
    ]):
        log.info(f"    {r['n']:>7,}  {r['_id']}")
    log.info("─────────────────────────────────────────────────────────")

    cliente.close()
    log.info("Listo.")


if __name__ == "__main__":
    main()