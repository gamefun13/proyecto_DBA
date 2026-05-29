"""
04 - Reproducible Rooftop Analysis Deduplicated
==============================================

Objetivo:
Comparar Google Open Buildings y Microsoft Building Footprints,
estimar número y área total de techos por municipio PDET,
evitando doble conteo entre fuentes.

Metodología:
- Google y Microsoft se analizan por municipio PDET.
- Se calcula área en CRS métrico EPSG:9377.
- Se detectan duplicados entre fuentes mediante solapamiento espacial.
- Google se usa como fuente prioritaria.
- Microsoft se agrega solo si no se considera duplicado de Google.

Salidas:
    outputs/rooftop_by_municipio_fuente.csv
    outputs/rooftop_by_municipio_deduplicated.csv
    outputs/quality_report.csv
    outputs/municipios_rooftop_deduplicated.geojson
    outputs/mapa_rooftop_count.png
    outputs/mapa_rooftop_area.png
    outputs/run_metadata.json

Uso:
    python 04_rooftop_analysis_deduplicated.py --mongo mongodb://localhost:27017/ --db proyecto --out outputs
"""

import argparse
import json
import logging
import os
import platform
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import geopandas as gpd
import pandas as pd
import pymongo
import shapely
from pymongo import MongoClient
from shapely.geometry import shape
from shapely.strtree import STRtree


# =========================
# CONFIGURACIÓN
# =========================
CRS_WGS84 = "EPSG:4326"
CRS_METRICO = "EPSG:9377"

COL_PDET = "territorios_pdet"
COL_GOOGLE = "google_buildings"
COL_MICROSOFT = "microsoft_buildings"

# Regla de duplicado:
# Si un polígono de Microsoft se solapa con uno de Google en al menos
# este porcentaje del área menor, se considera el mismo techo.
OVERLAP_MIN_RATIO = 0.50

# Prioridad de fuente:
# Si hay duplicado, se conserva Google y se descarta Microsoft.
FUENTE_PRIORITARIA = "google"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# =========================
# UTILIDADES
# =========================
def asegurar_carpeta(path: str):
    os.makedirs(path, exist_ok=True)


def geom_from_doc(doc: dict, field: str):
    geom_json = doc.get(field)

    if not geom_json:
        return None

    try:
        geom = shape(geom_json)
    except Exception:
        return None

    if geom is None or geom.is_empty:
        return None

    return geom


def cargar_municipios_pdet(db) -> gpd.GeoDataFrame:
    docs = list(
        db[COL_PDET].find(
            {},
            {
                "_id": 0,
                "codigo_mgn": 1,
                "nombre_municipio": 1,
                "departamento": 1,
                "subregion_pdet": 1,
                "geometria_pdet": 1,
            }
        )
    )

    registros = []

    for d in docs:
        geom = geom_from_doc(d, "geometria_pdet")

        if geom is None:
            continue

        registros.append(
            {
                "codigo_mgn": str(d.get("codigo_mgn")),
                "nombre_municipio": d.get("nombre_municipio"),
                "departamento": d.get("departamento"),
                "subregion_pdet": d.get("subregion_pdet"),
                "geometry": geom,
            }
        )

    gdf = gpd.GeoDataFrame(registros, geometry="geometry", crs=CRS_WGS84)

    return gdf


def cargar_edificios_municipio(db, collection: str, codigo_mgn: str, fuente: str) -> Tuple[gpd.GeoDataFrame, Dict]:
    """
    Carga edificios de una fuente para un municipio específico.
    """

    stats = {
        "fuente": fuente,
        "codigo_mgn": codigo_mgn,
        "total_docs": 0,
        "sin_geometria": 0,
        "geometria_invalida": 0,
        "geometrias_cargadas": 0,
    }

    cursor = db[collection].find(
        {"codigo_mgn": codigo_mgn},
        {
            "_id": 0,
            "codigo_mgn": 1,
            "nombre_municipio": 1,
            "departamento": 1,
            "subregion_pdet": 1,
            "geometria_edificio": 1,
            "area_estimada_m2": 1,
            "metadata": 1,
        }
    )

    registros = []

    for d in cursor:
        stats["total_docs"] += 1

        geom = geom_from_doc(d, "geometria_edificio")

        if geom is None:
            stats["sin_geometria"] += 1
            continue

        if not geom.is_valid:
            stats["geometria_invalida"] += 1
            continue

        registros.append(
            {
                "fuente": fuente,
                "codigo_mgn": str(d.get("codigo_mgn")),
                "nombre_municipio": d.get("nombre_municipio"),
                "departamento": d.get("departamento"),
                "subregion_pdet": d.get("subregion_pdet"),
                "area_estimada_m2": d.get("area_estimada_m2"),
                "geometry": geom,
            }
        )

        stats["geometrias_cargadas"] += 1

    if not registros:
        gdf = gpd.GeoDataFrame(
            columns=[
                "fuente",
                "codigo_mgn",
                "nombre_municipio",
                "departamento",
                "subregion_pdet",
                "area_estimada_m2",
                "geometry",
            ],
            geometry="geometry",
            crs=CRS_WGS84,
        )

        return gdf, stats

    gdf = gpd.GeoDataFrame(registros, geometry="geometry", crs=CRS_WGS84)

    return gdf, stats


def calcular_area_m2(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Calcula área en m2.
    Si la fuente trae area_estimada_m2, se conserva como referencia,
    pero se crea area_calc_m2 usando geometría proyectada.
    """

    if gdf.empty:
        gdf["area_calc_m2"] = []
        return gdf

    gdf_metric = gdf.to_crs(CRS_METRICO)

    gdf = gdf.copy()
    gdf["area_calc_m2"] = gdf_metric.geometry.area.round(2)

    if "area_estimada_m2" in gdf.columns:
        gdf["area_estimada_m2"] = pd.to_numeric(
            gdf["area_estimada_m2"],
            errors="coerce"
        )

    return gdf


def marcar_duplicados_ms_con_google(
    gdf_google: gpd.GeoDataFrame,
    gdf_msft: gpd.GeoDataFrame,
    overlap_min_ratio: float = OVERLAP_MIN_RATIO,
) -> Tuple[gpd.GeoDataFrame, Dict]:
    """
    Marca edificios Microsoft que parecen duplicados de Google.

    Regla:
    Un edificio Microsoft es duplicado si intersecta un edificio Google y:

        area_interseccion / min(area_google, area_msft) >= overlap_min_ratio

    Se compara en CRS métrico para que las áreas sean correctas.
    """

    stats = {
        "microsoft_evaluados": len(gdf_msft),
        "duplicados_ms_google": 0,
        "microsoft_unicos": 0,
    }

    if gdf_msft.empty:
        gdf_msft = gdf_msft.copy()
        gdf_msft["duplicado_google"] = []
        return gdf_msft, stats

    if gdf_google.empty:
        gdf_msft = gdf_msft.copy()
        gdf_msft["duplicado_google"] = False
        stats["microsoft_unicos"] = len(gdf_msft)
        return gdf_msft, stats

    google_m = gdf_google.to_crs(CRS_METRICO).reset_index(drop=True)
    msft_m = gdf_msft.to_crs(CRS_METRICO).reset_index(drop=True)

    google_geoms = list(google_m.geometry)
    google_areas = list(google_m.geometry.area)

    tree = STRtree(google_geoms)

    duplicados = []

    for idx, row in msft_m.iterrows():
        geom_ms = row.geometry

        if geom_ms is None or geom_ms.is_empty:
            duplicados.append(False)
            continue

        area_ms = geom_ms.area

        if area_ms <= 0:
            duplicados.append(False)
            continue

        candidatos = tree.query(geom_ms)

        es_duplicado = False

        for cand in candidatos:
            # Shapely 2.x devuelve índices; Shapely 1.x devuelve geometrías
            try:
                idx_g = int(cand)
                geom_g = google_geoms[idx_g]
                area_g = google_areas[idx_g]
            except Exception:
                geom_g = cand
                try:
                    idx_g = google_geoms.index(geom_g)
                    area_g = google_areas[idx_g]
                except Exception:
                    area_g = geom_g.area

            if not geom_ms.intersects(geom_g):
                continue

            inter_area = geom_ms.intersection(geom_g).area

            if inter_area <= 0:
                continue

            denom = min(area_ms, area_g)

            if denom <= 0:
                continue

            overlap_ratio = inter_area / denom

            if overlap_ratio >= overlap_min_ratio:
                es_duplicado = True
                break

        duplicados.append(es_duplicado)

    gdf_msft = gdf_msft.copy()
    gdf_msft["duplicado_google"] = duplicados

    stats["duplicados_ms_google"] = int(sum(duplicados))
    stats["microsoft_unicos"] = int(len(gdf_msft) - sum(duplicados))

    return gdf_msft, stats


def resumen_fuente(gdf: gpd.GeoDataFrame, fuente: str, codigo_mgn: str, nombre_municipio: str) -> dict:
    if gdf.empty:
        return {
            "fuente": fuente,
            "codigo_mgn": codigo_mgn,
            "nombre_municipio": nombre_municipio,
            "n_edificios": 0,
            "area_total_m2": 0.0,
        }

    return {
        "fuente": fuente,
        "codigo_mgn": codigo_mgn,
        "nombre_municipio": nombre_municipio,
        "n_edificios": int(len(gdf)),
        "area_total_m2": round(float(gdf["area_calc_m2"].sum()), 2),
    }


def generar_mapas(gdf_mun: gpd.GeoDataFrame, out_dir: str):
    import matplotlib.pyplot as plt

    # Mapa conteo
    fig, ax = plt.subplots(1, 1, figsize=(10, 12))
    gdf_mun.plot(
        column="total_edificios_sin_duplicados",
        ax=ax,
        legend=True,
        cmap="OrRd",
    )
    ax.set_title("Edificios sin duplicados por municipio PDET")
    ax.axis("off")

    out_count = os.path.join(out_dir, "mapa_rooftop_count.png")
    fig.savefig(out_count, dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Mapa área
    fig, ax = plt.subplots(1, 1, figsize=(10, 12))
    gdf_mun.plot(
        column="total_area_m2_sin_duplicados",
        ax=ax,
        legend=True,
        cmap="YlGnBu",
    )
    ax.set_title("Área total de techos sin duplicados por municipio PDET")
    ax.axis("off")

    out_area = os.path.join(out_dir, "mapa_rooftop_area.png")
    fig.savefig(out_area, dpi=150, bbox_inches="tight")
    plt.close(fig)

    return out_count, out_area


# =========================
# MAIN
# =========================
def main():
    parser = argparse.ArgumentParser(
        description="Rooftop count and area estimation for PDET municipalities without duplicates."
    )

    parser.add_argument(
        "--mongo",
        default="mongodb://localhost:27017/",
        help="MongoDB URI"
    )

    parser.add_argument(
        "--db",
        default="proyecto",
        help="MongoDB database name"
    )

    parser.add_argument(
        "--out",
        default="outputs",
        help="Output folder"
    )

    parser.add_argument(
        "--overlap",
        type=float,
        default=OVERLAP_MIN_RATIO,
        help="Minimum overlap ratio to classify Microsoft building as duplicate of Google"
    )

    args = parser.parse_args()

    asegurar_carpeta(args.out)

    cliente = MongoClient(args.mongo)
    db = cliente[args.db]

    log.info("Cargando municipios PDET...")
    gdf_mun = cargar_municipios_pdet(db)

    if gdf_mun.empty:
        raise RuntimeError("No se encontraron municipios PDET.")

    log.info(f"Municipios PDET cargados: {len(gdf_mun)}")

    filas_fuente = []
    filas_finales = []
    filas_quality = []

    total_google_global = 0
    total_msft_global = 0
    total_msft_duplicados_global = 0
    total_final_global = 0

    for i, mun in gdf_mun.iterrows():
        codigo_mgn = str(mun["codigo_mgn"])
        nombre_municipio = mun["nombre_municipio"]

        log.info(
            f"[{i + 1}/{len(gdf_mun)}] Procesando {nombre_municipio} ({codigo_mgn})..."
        )

        gdf_google, stats_google = cargar_edificios_municipio(
            db,
            COL_GOOGLE,
            codigo_mgn,
            "google"
        )

        gdf_msft, stats_msft = cargar_edificios_municipio(
            db,
            COL_MICROSOFT,
            codigo_mgn,
            "microsoft"
        )

        gdf_google = calcular_area_m2(gdf_google)
        gdf_msft = calcular_area_m2(gdf_msft)

        gdf_msft, stats_dedupe = marcar_duplicados_ms_con_google(
            gdf_google,
            gdf_msft,
            overlap_min_ratio=args.overlap,
        )

        gdf_msft_unicos = gdf_msft[
            gdf_msft["duplicado_google"] == False
        ].copy()

        # Resumen por fuente original
        filas_fuente.append(
            resumen_fuente(
                gdf_google,
                "google",
                codigo_mgn,
                nombre_municipio
            )
        )

        filas_fuente.append(
            resumen_fuente(
                gdf_msft,
                "microsoft",
                codigo_mgn,
                nombre_municipio
            )
        )

        # Resumen final sin duplicados
        n_google = len(gdf_google)
        area_google = float(gdf_google["area_calc_m2"].sum()) if not gdf_google.empty else 0.0

        n_msft_original = len(gdf_msft)
        area_msft_original = float(gdf_msft["area_calc_m2"].sum()) if not gdf_msft.empty else 0.0

        n_msft_duplicados = int(stats_dedupe["duplicados_ms_google"])
        n_msft_unicos = len(gdf_msft_unicos)
        area_msft_unica = float(gdf_msft_unicos["area_calc_m2"].sum()) if not gdf_msft_unicos.empty else 0.0

        total_sin_dup = n_google + n_msft_unicos
        area_sin_dup = area_google + area_msft_unica

        filas_finales.append(
            {
                "codigo_mgn": codigo_mgn,
                "nombre_municipio": nombre_municipio,
                "departamento": mun.get("departamento"),
                "subregion_pdet": mun.get("subregion_pdet"),

                "google_edificios": int(n_google),
                "google_area_m2": round(area_google, 2),

                "microsoft_edificios_original": int(n_msft_original),
                "microsoft_area_m2_original": round(area_msft_original, 2),

                "microsoft_duplicados_con_google": int(n_msft_duplicados),
                "microsoft_unicos_agregados": int(n_msft_unicos),
                "microsoft_area_m2_unica_agregada": round(area_msft_unica, 2),

                "total_edificios_sin_duplicados": int(total_sin_dup),
                "total_area_m2_sin_duplicados": round(area_sin_dup, 2),

                "overlap_min_ratio": args.overlap,
                "fuente_prioritaria": FUENTE_PRIORITARIA,
            }
        )

        filas_quality.append(
            {
                "codigo_mgn": codigo_mgn,
                "nombre_municipio": nombre_municipio,

                "google_total_docs": stats_google["total_docs"],
                "google_geometrias_cargadas": stats_google["geometrias_cargadas"],
                "google_sin_geometria": stats_google["sin_geometria"],
                "google_geometria_invalida": stats_google["geometria_invalida"],

                "microsoft_total_docs": stats_msft["total_docs"],
                "microsoft_geometrias_cargadas": stats_msft["geometrias_cargadas"],
                "microsoft_sin_geometria": stats_msft["sin_geometria"],
                "microsoft_geometria_invalida": stats_msft["geometria_invalida"],

                "microsoft_duplicados_google": n_msft_duplicados,
                "microsoft_unicos": n_msft_unicos,
                "tasa_duplicacion_ms_google": round(
                    n_msft_duplicados / max(1, n_msft_original),
                    6
                ),
            }
        )

        total_google_global += n_google
        total_msft_global += n_msft_original
        total_msft_duplicados_global += n_msft_duplicados
        total_final_global += total_sin_dup

        log.info(
            f"  Google: {n_google:,} | "
            f"Microsoft: {n_msft_original:,} | "
            f"Duplicados MS-Google: {n_msft_duplicados:,} | "
            f"Final sin duplicados: {total_sin_dup:,}"
        )

    # =========================
    # DATAFRAMES DE SALIDA
    # =========================
    df_fuente = pd.DataFrame(filas_fuente)
    df_final = pd.DataFrame(filas_finales)
    df_quality = pd.DataFrame(filas_quality)

    # =========================
    # GUARDAR TABLAS
    # =========================
    out_fuente = os.path.join(args.out, "rooftop_by_municipio_fuente.csv")
    out_final = os.path.join(args.out, "rooftop_by_municipio_deduplicated.csv")
    out_quality = os.path.join(args.out, "quality_report.csv")

    df_fuente.to_csv(out_fuente, index=False, encoding="utf-8-sig")
    df_final.to_csv(out_final, index=False, encoding="utf-8-sig")
    df_quality.to_csv(out_quality, index=False, encoding="utf-8-sig")

    log.info(f"Guardado: {out_fuente}")
    log.info(f"Guardado: {out_final}")
    log.info(f"Guardado: {out_quality}")

    # =========================
    # GEOJSON MUNICIPAL CON RESULTADOS
    # =========================
    gdf_out = gdf_mun.merge(
        df_final,
        on=["codigo_mgn", "nombre_municipio", "departamento", "subregion_pdet"],
        how="left"
    )

    columnas_numericas = [
        "google_edificios",
        "google_area_m2",
        "microsoft_edificios_original",
        "microsoft_area_m2_original",
        "microsoft_duplicados_con_google",
        "microsoft_unicos_agregados",
        "microsoft_area_m2_unica_agregada",
        "total_edificios_sin_duplicados",
        "total_area_m2_sin_duplicados",
    ]

    for col in columnas_numericas:
        if col in gdf_out.columns:
            gdf_out[col] = gdf_out[col].fillna(0)

    geojson_out = os.path.join(args.out, "municipios_rooftop_deduplicated.geojson")
    gdf_out.to_file(geojson_out, driver="GeoJSON", encoding="utf-8")

    log.info(f"Guardado: {geojson_out}")

    # =========================
    # MAPAS
    # =========================
    try:
        mapa_count, mapa_area = generar_mapas(gdf_out, args.out)
        log.info(f"Guardado: {mapa_count}")
        log.info(f"Guardado: {mapa_area}")
    except Exception as e:
        log.warning(f"No se pudieron generar mapas: {e}")

    # =========================
    # METADATOS REPRODUCIBILIDAD
    # =========================
    metadata = {
        "script": "04_rooftop_analysis_deduplicated.py",
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "mongo_uri": args.mongo,
        "database": args.db,
        "collections": {
            "municipios_pdet": COL_PDET,
            "google_buildings": COL_GOOGLE,
            "microsoft_buildings": COL_MICROSOFT,
        },
        "methodology": {
            "goal": "Estimate count and total rooftop area per PDET municipality.",
            "municipality_assignment": "Buildings were previously assigned to PDET municipalities through spatial containment / centroid coverage during loading.",
            "area_calculation": f"Areas calculated using projected CRS {CRS_METRICO}.",
            "deduplication": {
                "rule": "A Microsoft building is classified as duplicate when it spatially overlaps a Google building by intersection_area / min(area_google, area_microsoft) >= threshold.",
                "threshold": args.overlap,
                "priority": "Google is kept as primary source; non-duplicate Microsoft buildings are added.",
            },
            "outputs": [
                "rooftop_by_municipio_fuente.csv",
                "rooftop_by_municipio_deduplicated.csv",
                "quality_report.csv",
                "municipios_rooftop_deduplicated.geojson",
                "mapa_rooftop_count.png",
                "mapa_rooftop_area.png",
            ],
        },
        "software_versions": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "geopandas": gpd.__version__,
            "shapely": shapely.__version__,
            "pymongo": pymongo.version,
        },
        "global_summary": {
            "google_original_buildings": int(total_google_global),
            "microsoft_original_buildings": int(total_msft_global),
            "microsoft_duplicates_with_google": int(total_msft_duplicados_global),
            "final_buildings_without_duplicates": int(total_final_global),
        },
    }

    metadata_out = os.path.join(args.out, "run_metadata.json")

    with open(metadata_out, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    log.info(f"Guardado: {metadata_out}")

    log.info("─── RESUMEN GLOBAL ─────────────────────────────────────")
    log.info(f"Google original:              {total_google_global:,}")
    log.info(f"Microsoft original:           {total_msft_global:,}")
    log.info(f"Duplicados Microsoft-Google:  {total_msft_duplicados_global:,}")
    log.info(f"Final sin duplicados:         {total_final_global:,}")
    log.info("────────────────────────────────────────────────────────")
    log.info("=== Análisis reproducible completado ✓ ===")

    cliente.close()


if __name__ == "__main__":
    main()