"""
Carga archivos Microsoft Global ML Building Footprints (.geojson o .geojson.gz) → MongoDB
==========================================================================================
Base de datos : proyecto
Colección     : microsoft_buildings

Instrucciones:
    1. Poner los archivos .geojson (o .geojson.gz) en la misma carpeta
    2. Tener MongoDB corriendo
    3. Ejecutar: python cargar_microsoft_buildings.py
"""

import os
import glob
import gzip
import json
import logging
import geopandas as gpd
from shapely.geometry import shape, mapping
from shapely.strtree import STRtree
from pymongo import MongoClient, GEOSPHERE, InsertOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MONGO_URI  = "mongodb://localhost:27017/"
DB_NAME    = "proyecto"
COLLECTION = "microsoft_buildings"
BATCH_SIZE = 2000


def leer_geojson(path):
    """Lee un archivo GeoJSON plano o comprimido con gzip."""
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def extraer_props(feature):
    """
    Extrae height y confidence tolerando propiedades simples o doble-anidadas.
    Ejemplo doble-anidado: {"properties": {"type": "Feature", "properties": {...}}}
    """
    props = feature.get("properties") or {}
    if "properties" in props:
        props = props["properties"]
    height     = props.get("height",     -1)
    confidence = props.get("confidence", -1)
    return height, confidence


def main():
    log.info("=== Cargando Microsoft Buildings → MongoDB (proyecto) ===")

    # ── Buscar todos los GeoJSON en la carpeta actual ────────────────────────
    archivos = (
        glob.glob("*.geojson.gz") +
        glob.glob("*.geojson") +
        glob.glob("*.json")
    )
    archivos = list(set(archivos))

    if not archivos:
        log.error("No se encontraron archivos .geojson / .geojson.gz / .json en esta carpeta.")
        return

    log.info(f"Archivos encontrados: {len(archivos)}")
    for a in sorted(archivos):
        log.info(f"  {a}")

    # ── Conectar a MongoDB y cargar municipios PDET ──────────────────────────
    cliente = MongoClient(MONGO_URI)
    db      = cliente[DB_NAME]

    docs = list(db["territorios_pdet"].find(
        {}, {"codigo_mgn": 1, "nombre_municipio": 1, "geometria_pdet": 1}
    ))

    if not docs:
        log.error("No hay municipios en 'territorios_pdet'. Corre primero el script de PDET.")
        cliente.close()
        return

    # Construir índice espacial con los municipios
    municipios = []
    for d in docs:
        geom = shape(d["geometria_pdet"])
        municipios.append({
            "codigo_mgn":       d["codigo_mgn"],
            "nombre_municipio": d["nombre_municipio"],
            "geometry":         geom,
        })

    gdf   = gpd.GeoDataFrame(municipios, crs="EPSG:4326")
    geoms = list(gdf.geometry)
    tree  = STRtree(geoms)
    log.info(f"Municipios PDET cargados: {len(gdf)}")

    # ── Preparar colección e índices ─────────────────────────────────────────
    col     = db[COLLECTION]
    indices = [i["name"] for i in col.list_indexes()]

    if "idx_geo_edificio" not in indices:
        col.create_index([("geometria_edificio", GEOSPHERE)], name="idx_geo_edificio")
        col.create_index("fuente",     name="idx_fuente")
        col.create_index("codigo_mgn", name="idx_codigo_mgn")
        log.info("Indices creados.")

    total_global    = 0
    omitidos_global = 0

    # ── Procesar cada archivo ────────────────────────────────────────────────
    for archivo in sorted(archivos):
        log.info(f"Procesando {archivo} ...")
        try:
            geojson = leer_geojson(archivo)
        except Exception as e:
            log.warning(f"  Error leyendo {archivo}: {e}")
            continue

        features_raw = geojson.get("features", [])
        log.info(f"  {len(features_raw):,} features en el archivo")

        batch    = []
        count    = 0
        omitidos = 0

        for feature in features_raw:
            try:
                # Geometria
                geom_raw = feature.get("geometry")
                if not geom_raw:
                    omitidos += 1
                    continue

                geom = shape(geom_raw)
                if geom is None or geom.is_empty or not geom.is_valid:
                    omitidos += 1
                    continue

                # Cruce espacial con municipios PDET
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
                    omitidos += 1
                    continue

                height, confidence = extraer_props(feature)

                doc = {
                    "fuente":             "microsoft",
                    "codigo_mgn":         municipio_match["codigo_mgn"],
                    "nombre_municipio":   municipio_match["nombre_municipio"],
                    "geometria_edificio": mapping(geom),
                    "metadata": {
                        "altura_m":        float(height)     if height     not in (-1, None) else None,
                        "confianza":       float(confidence) if confidence not in (-1, None) else None,
                        "fuente_imagen":   "Microsoft Bing Imagery",
                        "dataset_version": "Microsoft Global ML Building Footprints",
                    },
                }
                batch.append(InsertOne(doc))
                count += 1

                if len(batch) >= BATCH_SIZE:
                    col.bulk_write(batch, ordered=False)
                    batch = []

            except Exception as e:
                log.debug(f"  Feature omitida por error: {e}")
                omitidos += 1
                continue

        if batch:
            col.bulk_write(batch, ordered=False)

        log.info(f"  -> {count:,} edificios PDET insertados | {omitidos:,} omitidos (fuera de PDET o invalidos)")
        total_global    += count
        omitidos_global += omitidos

    # ── Resumen final ────────────────────────────────────────────────────────
    log.info("─── Resumen ─────────────────────────────────────────────")
    log.info(f"  Archivos procesados:                     {len(archivos)}")
    log.info(f"  Total edificios insertados:              {total_global:,}")
    log.info(f"  Total omitidos (fuera PDET o invalidos): {omitidos_global:,}")
    log.info(f"  Total en coleccion:                      {col.count_documents({}):,}")
    log.info("  Top 10 municipios:")
    for r in col.aggregate([
        {"$group": {"_id": "$nombre_municipio", "n": {"$sum": 1}}},
        {"$sort":  {"n": -1}},
        {"$limit": 10}
    ]):
        log.info(f"    {r['n']:>7,}  {r['_id']}")
    log.info("─────────────────────────────────────────────────────────")

    cliente.close()
    log.info("Listo.")


if __name__ == "__main__":
    main()