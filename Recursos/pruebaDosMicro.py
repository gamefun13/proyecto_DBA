"""
Descarga todos los cuadrantes de Microsoft Buildings para Colombia
==================================================================
Basado en el snippet oficial de Microsoft.
Guarda cada cuadrante como .geojson en la carpeta microsoft_geojson/

Ejecutar: python descargar_microsoft_colombia.py
"""

import os
import logging
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

INDEX_URL     = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
LOCATION      = "Colombia"
OUTPUT_FOLDER = "microsoft_geojson"


def main():
    log.info("=== Descarga Microsoft Buildings — Colombia ===")

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    # ── 1. Descargar índice oficial de Microsoft ─────────────────────────────
    log.info("Descargando índice de cuadrantes...")
    dataset_links  = pd.read_csv(INDEX_URL)
    colombia_links = dataset_links[dataset_links.Location == LOCATION].reset_index(drop=True)
    log.info(f"Cuadrantes de {LOCATION}: {len(colombia_links)}")

    # ── 2. Descargar cada cuadrante ──────────────────────────────────────────
    ok      = 0
    errores = 0

    for i, row in colombia_links.iterrows():
        quadkey = row.QuadKey
        url     = row.Url
        destino = os.path.join(OUTPUT_FOLDER, f"{quadkey}.geojson")

        # Si ya existe, saltar
        if os.path.exists(destino):
            log.info(f"  [{i+1}/{len(colombia_links)}] Ya existe: {quadkey}.geojson")
            ok += 1
            continue

        try:
            log.info(f"  [{i+1}/{len(colombia_links)}] Descargando: {quadkey} ...")

            # Igual que el snippet oficial de Microsoft
            df = pd.read_json(url, lines=True)
            df["geometry"] = df["geometry"].apply(shape)
            gdf = gpd.GeoDataFrame(df, crs=4326)
            gdf.to_file(destino, driver="GeoJSON")

            log.info(f"    -> {len(gdf):,} edificios guardados en {quadkey}.geojson")
            ok += 1

        except Exception as e:
            log.warning(f"  [{i+1}/{len(colombia_links)}] Error en {quadkey}: {e}")
            errores += 1
            continue

    # ── 3. Resumen ───────────────────────────────────────────────────────────
    log.info("─── Resumen ─────────────────────────────────────────────")
    log.info(f"  Cuadrantes totales   : {len(colombia_links)}")
    log.info(f"  Descargados / ya OK  : {ok}")
    log.info(f"  Errores              : {errores}")
    log.info(f"  Archivos en          : {OUTPUT_FOLDER}/")
    log.info("─────────────────────────────────────────────────────────")
    log.info("Listo. Ahora corre cargar_microsoft_buildings.py apuntando a la carpeta microsoft_geojson/")


if __name__ == "__main__":
    main()