"""
SEMANA 3 - Script 1: Carga de Municipios PDET a MongoDB
========================================================
Carga el GeoJSON ya generado en Semana 2 (pdet_final.geojson)
directamente a MongoDB con índice espacial 2dsphere.

Dataset: 164 municipios PDET en 16 subregiones
Fuente geometrías: MGN DANE 2025

Requisitos:
    pip install pymongo geopandas pyogrio

Uso:
    Copiar pdet_final.geojson en la misma carpeta que este script y ejecutar:
    python 01_cargar_pdet_mongodb.py
"""

import logging
from pathlib import Path

import geopandas as gpd
from pymongo import MongoClient, GEOSPHERE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuración ──────────────────────────────────────────────────────────────
MONGO_URI    = "mongodb://localhost:27017/"
DB_NAME      = "proyecto"
COLLECTION   = "territorios_pdet"
GEOJSON_PATH = Path(__file__).with_name("pdet_final.geojson")


# ─── Funciones ──────────────────────────────────────────────────────────────────

def cargar_geojson(path: str) -> list:
    """Lee el GeoJSON PDET y construye documentos para MongoDB."""
    log.info(f"Leyendo {path} ...")
    gdf = gpd.read_file(path)

    # Asegurar CRS WGS84 (requerido por MongoDB 2dsphere)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        log.info(f"Reproyectando de EPSG:{gdf.crs.to_epsg()} a EPSG:4326 ...")
        gdf = gdf.to_crs(epsg=4326)

    log.info(f"Municipios PDET leídos: {len(gdf)}")
    log.info(f"Subregiones: {gdf['Subregión PDET'].nunique()}")

    # Calcular área en km² en CRS métrico colombiano
    gdf_metro  = gdf.to_crs(epsg=9377)   # MAGNA-SIRGAS / Colombia Origen Nacional
    gdf["area_km2"] = (gdf_metro.geometry.area / 1e6).round(4)

    documentos = []
    for _, row in gdf.iterrows():
        geom = row.geometry.__geo_interface__
        doc = {
            "codigo_mgn":        str(row["mpio_cdpmp"]).zfill(5),
            "codigo_dpto":       str(row["dpto_ccdgo"]).zfill(2),
            "nombre_municipio":  row["Municipio"],
            "nombre_dpto":       row["Departamento"],
            "subregion_pdet":    row["Subregión PDET"],
            "geometria_pdet":    geom,
            "datos_pdet": {
                "es_pdet":              True,
                "fuente_limites":       "MGN DANE 2025",
                "area_km2":             float(row["area_km2"]),
                "mpio_tipo":            row.get("mpio_tipo", "MUNICIPIO"),
                "shape_area_grados":    float(row["shape_Area"]),
            },
        }
        documentos.append(doc)

    return documentos


def cargar_mongodb(documentos: list):
    """Inserta documentos en MongoDB y crea índices espaciales y de búsqueda."""
    cliente = MongoClient(MONGO_URI)
    db      = cliente[DB_NAME]
    col     = db[COLLECTION]

    col.drop()
    log.info(f"Colección '{COLLECTION}' reiniciada.")

    col.insert_many(documentos)
    log.info(f"Insertados {col.count_documents({})} documentos.")

    # Índice espacial 2dsphere (obligatorio para $geoIntersects, $near, etc.)
    col.create_index([("geometria_pdet", GEOSPHERE)],  name="idx_geo_pdet")
    col.create_index("codigo_mgn",  unique=True,        name="idx_codigo_mgn")
    col.create_index("subregion_pdet",                  name="idx_subregion")
    col.create_index("nombre_municipio",                name="idx_nombre")
    col.create_index("codigo_dpto",                     name="idx_dpto")

    log.info("Índices creados: 2dsphere, codigo_mgn (único), subregion_pdet, nombre_municipio, codigo_dpto")
    cliente.close()


def verificar_carga():
    """Verifica la carga con queries de comprobación."""
    cliente = MongoClient(MONGO_URI)
    col     = cliente[DB_NAME][COLLECTION]

    total       = col.count_documents({})
    subregiones = len(col.distinct("subregion_pdet"))
    dptos       = len(col.distinct("codigo_dpto"))

    log.info("─── Verificación de carga ───────────────────────────────")
    log.info(f"  Total municipios PDET:  {total}")
    log.info(f"  Subregiones PDET:       {subregiones}")
    log.info(f"  Departamentos:          {dptos}")

    log.info("\n  Municipios por subregión:")
    for r in col.aggregate([{"$group": {"_id": "$subregion_pdet", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}]):
        log.info(f"    {r['n']:3d}  {r['_id']}")

    # Test de query espacial: punto en Quibdó
    hit = col.find_one({
        "geometria_pdet": {
            "$geoIntersects": {"$geometry": {"type": "Point", "coordinates": [-76.6531, 5.6919]}}
        }
    })
    if hit:
        log.info(f"\n  ✓ Query espacial OK → punto en Quibdó resuelto en: {hit['nombre_municipio']}")
    else:
        log.warning("\n  ⚠ Query espacial sin resultado para Quibdó")
    log.info("─────────────────────────────────────────────────────────")
    cliente.close()


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    log.info("=== SCRIPT 1: Carga de Municipios PDET → MongoDB ===")
    documentos = cargar_geojson(GEOJSON_PATH)
    cargar_mongodb(documentos)
    verificar_carga()
    log.info("=== Script 1 completado ✓ ===")


if __name__ == "__main__":
    main()
