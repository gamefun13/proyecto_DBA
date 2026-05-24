"""
02 - Reproducible Rooftop Analysis
=================================
Genera conteo y área total de techos por municipio PDET.

Salida:
    - rooftop_by_municipio_fuente.csv : conteo y área por municipio y fuente
    - rooftop_by_municipio.csv        : totales por municipio
    - quality_report.csv              : métricas de calidad/accuracy espacial
    - municipios_rooftop.geojson      : polígonos PDET con atributos de conteo/área
    - mapa_rooftop.png                : mapa PNG de cobertura/área
    - run_metadata.json               : metodología, parámetros y versiones

Uso:
  python 02_rooftop_analysis.py --mongo mongodb://localhost:27017/ --out outputs

Requisitos: pymongo geopandas shapely pandas pyproj matplotlib
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Tuple

import geopandas as gpd
import pandas as pd
import pyproj
import shapely
import pymongo
from pymongo import MongoClient
from shapely.geometry import shape

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def mongo_collection_to_gdf(
    client: MongoClient,
    db: str,
    collection: str,
    geom_field: str = "geometria_edificio",
) -> Tuple[gpd.GeoDataFrame, Dict[str, int]]:
    """Lee una colección de MongoDB y devuelve un GeoDataFrame con métricas de calidad."""
    cur = client[db][collection].find({}, {"_id": 0})
    records = []
    stats = {
        "total_docs": 0,
        "sin_geometria": 0,
        "geometria_invalida": 0,
        "geometrias_cargadas": 0,
    }
    for d in cur:
        stats["total_docs"] += 1
        geom = d.get(geom_field) or d.get("geometria") or d.get("geometry")
        if not geom:
            stats["sin_geometria"] += 1
            continue
        try:
            g = shape(geom)
        except Exception:
            stats["geometria_invalida"] += 1
            continue
        d_copy = dict(d)
        d_copy["geometry"] = g
        records.append(d_copy)
        stats["geometrias_cargadas"] += 1

    if not records:
        return gpd.GeoDataFrame(columns=["geometry"]).set_crs(epsg=4326), stats

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    return gdf, stats


def compute_area_m2(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Calcula área en metros cuadrados usando CRS métrico (EPSG:9377).
    Conserva valores existentes en columna `area_estimada_m2` si están presentes.
    """
    if "area_estimada_m2" in gdf.columns:
        # usar campo existente cuando no nulo
        existing = gdf["area_estimada_m2"].replace({None: pd.NA})
    else:
        existing = pd.Series([pd.NA] * len(gdf))

    # calcular área desde geometría cuando sea necesario
    gdf_metric = gdf.to_crs(epsg=9377)
    geom_areas = gdf_metric.geometry.area.round(2)

    # combinar: preferir existing si no nulo, sino usar geom_areas
    area = existing.fillna(geom_areas)
    return area.astype(float)


def main():
    parser = argparse.ArgumentParser(description="Rooftop count & area per PDET municipality")
    parser.add_argument("--mongo", default="mongodb://localhost:27017/", help="Mongo URI")
    parser.add_argument("--db", default="proyecto", help="MongoDB database name")
    parser.add_argument("--out", default="outputs", help="Output folder for CSVs and maps")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    cliente = MongoClient(args.mongo)
    db = cliente[args.db]

    log.info("Cargando municipios PDET desde MongoDB...")
    docs = list(db["territorios_pdet"].find({}, {"_id": 0, "geometria_pdet": 1, "codigo_mgn": 1, "nombre_municipio": 1}))
    if not docs:
        log.error("No hay municipios en 'territorios_pdet'. Ejecuta primero 01_cargar_pdet_mongodb.py")
        return

    mun_records = []
    for d in docs:
        try:
            geom = shape(d["geometria_pdet"])
        except Exception:
            continue
        mun_records.append({"codigo_mgn": d.get("codigo_mgn"), "nombre_municipio": d.get("nombre_municipio"), "geometry": geom})

    gdf_mun = gpd.GeoDataFrame(mun_records, geometry="geometry", crs="EPSG:4326")
    log.info(f"Municipios PDET cargados: {len(gdf_mun)}")

    # Cargar edificios Google y Microsoft
    log.info("Cargando edificios Google desde MongoDB...")
    gdf_google, stats_google = mongo_collection_to_gdf(cliente, args.db, "google_buildings", geom_field="geometria_edificio")
    log.info(f"Google: {len(gdf_google)} geometrías leídas")

    log.info("Cargando edificios Microsoft desde MongoDB...")
    gdf_msft, stats_msft = mongo_collection_to_gdf(cliente, args.db, "microsoft_buildings", geom_field="geometria_edificio")
    log.info(f"Microsoft: {len(gdf_msft)} geometrías leídas")

    # Calcular áreas
    if not gdf_google.empty:
        gdf_google["area_m2"] = compute_area_m2(gdf_google)
    if not gdf_msft.empty:
        gdf_msft["area_m2"] = compute_area_m2(gdf_msft)

    # Spatial join: asignar codigo_mgn desde municipio (busca dentro)
    log.info("Asignando municipios por cruce espacial (sjoin)...")
    if not gdf_google.empty:
        gg = gdf_google.sjoin(gdf_mun[ ["codigo_mgn", "nombre_municipio", "geometry"] ], how="left", predicate="within")
        gg = gg.rename(columns={"codigo_mgn_right": "codigo_mgn_mun", "nombre_municipio_right": "nombre_municipio_mun"})
        gg_asignados = int(gg["codigo_mgn_mun"].notna().sum())
        gg_no_asignados = int(gg["codigo_mgn_mun"].isna().sum())
    else:
        gg = gpd.GeoDataFrame(columns=["codigo_mgn_mun", "nombre_municipio_mun", "area_m2"]) 
        gg_asignados = 0
        gg_no_asignados = 0

    if not gdf_msft.empty:
        gm = gdf_msft.sjoin(gdf_mun[ ["codigo_mgn", "nombre_municipio", "geometry"] ], how="left", predicate="within")
        gm = gm.rename(columns={"codigo_mgn_right": "codigo_mgn_mun", "nombre_municipio_right": "nombre_municipio_mun"})
        gm_asignados = int(gm["codigo_mgn_mun"].notna().sum())
        gm_no_asignados = int(gm["codigo_mgn_mun"].isna().sum())
    else:
        gm = gpd.GeoDataFrame(columns=["codigo_mgn_mun", "nombre_municipio_mun", "area_m2"]) 
        gm_asignados = 0
        gm_no_asignados = 0

    # Agrupar por municipio
    log.info("Agregando conteos y áreas por municipio...")
    def agg_df(df: gpd.GeoDataFrame, fuente: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["fuente", "codigo_mgn", "nombre_municipio", "n_edificios", "area_total_m2"]).set_index("codigo_mgn")
        df2 = df.groupby(["codigo_mgn_mun", "nombre_municipio_mun"]).agg(
            n_edificios=("geometry", "count"),
            area_total_m2=("area_m2", "sum")
        ).reset_index()
        df2 = df2.rename(columns={"codigo_mgn_mun": "codigo_mgn", "nombre_municipio_mun": "nombre_municipio"})
        df2["fuente"] = fuente
        df2 = df2[["fuente", "codigo_mgn", "nombre_municipio", "n_edificios", "area_total_m2"]]
        df2 = df2.set_index("codigo_mgn")
        return df2

    df_google_agg = agg_df(gg, "google")
    df_msft_agg = agg_df(gm, "microsoft")

    # Unir y completar
    df_all = pd.concat([df_google_agg, df_msft_agg], axis=0, ignore_index=False)
    df_all = df_all.reset_index()

    # Agregado total por municipio (sumando fuentes)
    df_tot = df_all.groupby(["codigo_mgn", "nombre_municipio"]).agg(
        total_edificios=("n_edificios", "sum"),
        total_area_m2=("area_total_m2", "sum")
    ).reset_index()

    # Guardar outputs tabulares
    csv_by_source = os.path.join(args.out, "rooftop_by_municipio_fuente.csv")
    df_all.to_csv(csv_by_source, index=False, encoding="utf-8-sig")
    log.info(f"Guardado: {csv_by_source}")

    csv_out = os.path.join(args.out, "rooftop_by_municipio.csv")
    df_tot.to_csv(csv_out, index=False, encoding="utf-8-sig")
    log.info(f"Guardado: {csv_out}")

    # Métricas de exactitud/calidad de operaciones espaciales
    quality_rows = [
        {
            "fuente": "google",
            "total_docs": stats_google["total_docs"],
            "sin_geometria": stats_google["sin_geometria"],
            "geometria_invalida": stats_google["geometria_invalida"],
            "geometrias_cargadas": stats_google["geometrias_cargadas"],
            "asignadas_municipio": gg_asignados,
            "no_asignadas_municipio": gg_no_asignados,
            "tasa_asignacion": round((gg_asignados / max(1, stats_google["geometrias_cargadas"])), 6),
        },
        {
            "fuente": "microsoft",
            "total_docs": stats_msft["total_docs"],
            "sin_geometria": stats_msft["sin_geometria"],
            "geometria_invalida": stats_msft["geometria_invalida"],
            "geometrias_cargadas": stats_msft["geometrias_cargadas"],
            "asignadas_municipio": gm_asignados,
            "no_asignadas_municipio": gm_no_asignados,
            "tasa_asignacion": round((gm_asignados / max(1, stats_msft["geometrias_cargadas"])), 6),
        },
    ]
    df_quality = pd.DataFrame(quality_rows)
    quality_out = os.path.join(args.out, "quality_report.csv")
    df_quality.to_csv(quality_out, index=False, encoding="utf-8-sig")
    log.info(f"Guardado: {quality_out}")

    # Añadir atributos a municipios y exportar GeoJSON
    # Evitar solapamiento de columnas (p. ej. 'nombre_municipio') al unir;
    # unir sólo las columnas de totales desde df_tot
    df_tot_idx = df_tot.set_index("codigo_mgn")[["total_edificios", "total_area_m2"]]
    gdf_mun2 = gdf_mun.set_index("codigo_mgn").join(df_tot_idx, how="left")
    gdf_mun2["total_edificios"] = gdf_mun2["total_edificios"].fillna(0).astype(int)
    gdf_mun2["total_area_m2"] = gdf_mun2["total_area_m2"].fillna(0).astype(float)

    geojson_out = os.path.join(args.out, "municipios_rooftop.geojson")
    gdf_mun2.to_file(geojson_out, driver="GeoJSON", encoding="utf-8")
    log.info(f"Guardado: {geojson_out}")

    # Mapa rápido
    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, 1, figsize=(10, 12))
        try:
            # `scheme` requiere mapclassify; si no está instalado, usar fallback sin clasificación
            gdf_mun2.plot(column="total_edificios", ax=ax, legend=True, cmap="OrRd", scheme="quantiles")
        except Exception as e:
            if "mapclassify" in str(e).lower():
                log.warning("`mapclassify` no está instalado; generando mapa sin esquema de cuantiles.")
                gdf_mun2.plot(column="total_edificios", ax=ax, legend=True, cmap="OrRd")
            else:
                raise
        ax.set_title("Total edificios por municipio (PDET)")
        ax.axis("off")
        mapa_out = os.path.join(args.out, "mapa_rooftop.png")
        fig.savefig(mapa_out, dpi=150, bbox_inches="tight")
        log.info(f"Guardado: {mapa_out}")
    except Exception as e:
        log.warning(f"No se pudo generar mapa PNG: {e}")

    # Metadatos de reproducibilidad/metodología para entrega
    metadata = {
        "analysis": "Rooftop Count and Area Estimation",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "mongo": args.mongo,
            "db": args.db,
            "out": args.out,
            "spatial_join_predicate": "within",
            "area_crs_epsg": 9377,
        },
        "inputs": {
            "municipios_pdet": len(gdf_mun),
            "google_geometrias_cargadas": int(len(gdf_google)),
            "microsoft_geometrias_cargadas": int(len(gdf_msft)),
        },
        "outputs": {
            "rooftop_by_municipio_fuente_csv": csv_by_source,
            "rooftop_by_municipio_csv": csv_out,
            "quality_report_csv": quality_out,
            "municipios_rooftop_geojson": geojson_out,
            "mapa_rooftop_png": os.path.join(args.out, "mapa_rooftop.png"),
        },
        "package_versions": {
            "python_pymongo": pymongo.__version__,
            "pandas": pd.__version__,
            "geopandas": gpd.__version__,
            "shapely": shapely.__version__,
            "pyproj": pyproj.__version__,
        },
        "methodology": [
            "Carga de geometrías PDET y edificios desde MongoDB.",
            "Cálculo de área en m2 en CRS métrico EPSG:9377.",
            "Asignación de edificio a municipio con sjoin predicate='within'.",
            "Agregación por fuente y total por municipio.",
            "Exportación de tablas (CSV), capa geoespacial (GeoJSON) y mapa (PNG).",
        ],
    }
    metadata_out = os.path.join(args.out, "run_metadata.json")
    with open(metadata_out, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    log.info(f"Guardado: {metadata_out}")

    cliente.close()
    log.info("Análisis completado.")


if __name__ == "__main__":
    main()
