"""
EDA - Auditoría Inicial de Datos de Edificaciones PDET
=======================================================
Semana 3 - Building Footprint Data Loading and Integration Report

Compara las colecciones google_buildings y microsoft_buildings
contra los territorios_pdet en MongoDB y genera:
  - eda_reporte_municipios.csv   : conteo y área por municipio y fuente
  - eda_resumen_general.csv      : estadísticas descriptivas globales
  - eda_top20_google.csv         : top 20 municipios por edificios (Google)
  - eda_top20_microsoft.csv      : top 20 municipios por edificios (Microsoft)
  - eda_comparativa.csv          : tabla lado a lado Google vs Microsoft

Ejecutar: python eda_semana3.py
"""

import logging
import pandas as pd
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MONGO_URI  = "mongodb://localhost:27017/"
DB_NAME    = "proyecto"


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def aggregate_to_df(col, pipeline):
    """Ejecuta un pipeline de agregación y devuelve un DataFrame."""
    return pd.DataFrame(list(col.aggregate(pipeline, allowDiskUse=True)))


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=== EDA - Auditoría Inicial de Datos (Semana 3) ===")

    cliente = MongoClient(MONGO_URI)
    db      = cliente[DB_NAME]

    col_pdet  = db["territorios_pdet"]
    col_goog  = db["google_buildings"]
    col_msft  = db["microsoft_buildings"]

    # ── 1. Totales generales ─────────────────────────────────────────────────
    log.info("1. Contando documentos totales...")

    n_municipios = col_pdet.count_documents({})
    n_google     = col_goog.count_documents({})
    n_microsoft  = col_msft.count_documents({})

    log.info(f"   Municipios PDET      : {n_municipios:>10,}")
    log.info(f"   Edificios Google     : {n_google:>10,}")
    log.info(f"   Edificios Microsoft  : {n_microsoft:>10,}")

    # ── 2. Agregación por municipio — Google ─────────────────────────────────
    log.info("2. Agregando Google por municipio...")

    pipeline_google = [
        {"$group": {
            "_id": {
                "codigo_mgn":       "$codigo_mgn",
                "nombre_municipio": "$nombre_municipio"
            },
            "n_edificios":      {"$sum": 1},
            "area_total_m2":    {"$sum": "$area_estimada_m2"},
            "area_promedio_m2": {"$avg": "$area_estimada_m2"},
            "area_min_m2":      {"$min": "$area_estimada_m2"},
            "area_max_m2":      {"$max": "$area_estimada_m2"},
            "conf_promedio":    {"$avg": "$metadata.confianza"},
        }},
        {"$sort": {"n_edificios": -1}}
    ]

    df_google = aggregate_to_df(col_goog, pipeline_google)

    if df_google.empty:
        log.warning("   No hay datos en google_buildings.")
        df_google = pd.DataFrame(columns=[
            "codigo_mgn", "nombre_municipio", "n_edificios",
            "area_total_m2", "area_promedio_m2", "area_min_m2",
            "area_max_m2", "conf_promedio"
        ])
    else:
        df_google["codigo_mgn"]       = df_google["_id"].apply(lambda x: x.get("codigo_mgn", ""))
        df_google["nombre_municipio"] = df_google["_id"].apply(lambda x: x.get("nombre_municipio", ""))
        df_google.drop(columns=["_id"], inplace=True)
        df_google["fuente"] = "google"
        df_google = df_google[[
            "fuente", "codigo_mgn", "nombre_municipio",
            "n_edificios", "area_total_m2", "area_promedio_m2",
            "area_min_m2", "area_max_m2", "conf_promedio"
        ]]

    log.info(f"   Municipios con datos Google     : {len(df_google):,}")

    # ── 3. Agregación por municipio — Microsoft ──────────────────────────────
    log.info("3. Agregando Microsoft por municipio...")

    pipeline_msft = [
        {"$group": {
            "_id": {
                "codigo_mgn":       "$codigo_mgn",
                "nombre_municipio": "$nombre_municipio"
            },
            "n_edificios":      {"$sum": 1},
            "conf_promedio":    {"$avg": "$metadata.confianza"},
            "altura_promedio_m":{"$avg": "$metadata.altura_m"},
        }},
        {"$sort": {"n_edificios": -1}}
    ]

    df_msft = aggregate_to_df(col_msft, pipeline_msft)

    if df_msft.empty:
        log.warning("   No hay datos en microsoft_buildings.")
        df_msft = pd.DataFrame(columns=[
            "codigo_mgn", "nombre_municipio", "n_edificios",
            "conf_promedio", "altura_promedio_m"
        ])
    else:
        df_msft["codigo_mgn"]        = df_msft["_id"].apply(lambda x: x.get("codigo_mgn", ""))
        df_msft["nombre_municipio"]  = df_msft["_id"].apply(lambda x: x.get("nombre_municipio", ""))
        df_msft.drop(columns=["_id"], inplace=True)
        df_msft["fuente"]            = "microsoft"
        # Microsoft no tiene area_estimada_m2 — se deja en None
        df_msft["area_total_m2"]     = None
        df_msft["area_promedio_m2"]  = None
        df_msft["area_min_m2"]       = None
        df_msft["area_max_m2"]       = None
        df_msft = df_msft[[
            "fuente", "codigo_mgn", "nombre_municipio",
            "n_edificios", "area_total_m2", "area_promedio_m2",
            "area_min_m2", "area_max_m2", "conf_promedio"
        ]]

    log.info(f"   Municipios con datos Microsoft  : {len(df_msft):,}")

    # ── 4. Reporte unificado por municipio ───────────────────────────────────
    log.info("4. Generando reporte unificado por municipio...")

    df_todos = pd.concat([df_google, df_msft], ignore_index=True)
    df_todos.sort_values(["nombre_municipio", "fuente"], inplace=True)
    df_todos.to_csv("eda_reporte_municipios.csv", index=False, encoding="utf-8-sig")
    log.info("   -> eda_reporte_municipios.csv")

    # ── 5. Tabla comparativa lado a lado ────────────────────────────────────
    log.info("5. Generando tabla comparativa Google vs Microsoft...")

    df_g_pivot = df_google[["codigo_mgn", "nombre_municipio", "n_edificios", "area_total_m2", "conf_promedio"]].copy()
    df_g_pivot.columns = ["codigo_mgn", "nombre_municipio", "google_n_edificios", "google_area_total_m2", "google_conf_promedio"]

    df_m_pivot = df_msft[["codigo_mgn", "nombre_municipio", "n_edificios"]].copy()
    df_m_pivot.columns = ["codigo_mgn", "nombre_municipio", "microsoft_n_edificios"]

    df_comp = pd.merge(df_g_pivot, df_m_pivot, on=["codigo_mgn", "nombre_municipio"], how="outer")
    df_comp["diferencia_n_edificios"] = df_comp["google_n_edificios"] - df_comp["microsoft_n_edificios"]
    df_comp["ratio_google_vs_msft"]   = (
        df_comp["google_n_edificios"] / df_comp["microsoft_n_edificios"]
    ).round(4)
    df_comp.sort_values("google_n_edificios", ascending=False, inplace=True)
    df_comp.to_csv("eda_comparativa.csv", index=False, encoding="utf-8-sig")
    log.info("   -> eda_comparativa.csv")

    # ── 6. Estadísticas descriptivas globales ────────────────────────────────
    log.info("6. Calculando estadísticas descriptivas globales...")

    # Google — estadísticas de área y confianza
    pipe_stats_google = [
        {"$group": {
            "_id": None,
            "total_edificios":  {"$sum": 1},
            "area_total_m2":    {"$sum": "$area_estimada_m2"},
            "area_promedio_m2": {"$avg": "$area_estimada_m2"},
            "area_min_m2":      {"$min": "$area_estimada_m2"},
            "area_max_m2":      {"$max": "$area_estimada_m2"},
            "conf_promedio":    {"$avg": "$metadata.confianza"},
            "conf_min":         {"$min": "$metadata.confianza"},
            "conf_max":         {"$max": "$metadata.confianza"},
        }}
    ]
    stats_g = list(col_goog.aggregate(pipe_stats_google))
    stats_g = stats_g[0] if stats_g else {}
    stats_g.pop("_id", None)
    stats_g["fuente"] = "google"
    stats_g["municipios_cubiertos"] = len(df_google)

    # Microsoft — estadísticas de confianza y altura
    pipe_stats_msft = [
        {"$group": {
            "_id": None,
            "total_edificios":    {"$sum": 1},
            "conf_promedio":      {"$avg": "$metadata.confianza"},
            "conf_min":           {"$min": "$metadata.confianza"},
            "conf_max":           {"$max": "$metadata.confianza"},
            "altura_promedio_m":  {"$avg": "$metadata.altura_m"},
            "altura_min_m":       {"$min": "$metadata.altura_m"},
            "altura_max_m":       {"$max": "$metadata.altura_m"},
        }}
    ]
    stats_m = list(col_msft.aggregate(pipe_stats_msft))
    stats_m = stats_m[0] if stats_m else {}
    stats_m.pop("_id", None)
    stats_m["fuente"] = "microsoft"
    stats_m["municipios_cubiertos"] = len(df_msft)

    df_stats = pd.DataFrame([stats_g, stats_m])
    # Redondear columnas numéricas
    num_cols = df_stats.select_dtypes(include="number").columns
    df_stats[num_cols] = df_stats[num_cols].round(2)
    df_stats.to_csv("eda_resumen_general.csv", index=False, encoding="utf-8-sig")
    log.info("   -> eda_resumen_general.csv")

    # ── 7. Top 20 por fuente ─────────────────────────────────────────────────
    log.info("7. Generando Top 20 por fuente...")

    df_google.head(20).to_csv("eda_top20_google.csv",    index=False, encoding="utf-8-sig")
    df_msft.head(20).to_csv("eda_top20_microsoft.csv",   index=False, encoding="utf-8-sig")
    log.info("   -> eda_top20_google.csv")
    log.info("   -> eda_top20_microsoft.csv")

    # ── 8. Municipios PDET sin cobertura ────────────────────────────────────
    log.info("8. Detectando municipios PDET sin cobertura...")

    todos_pdet = set(
        d["codigo_mgn"] for d in col_pdet.find({}, {"codigo_mgn": 1})
    )
    con_google = set(df_google["codigo_mgn"].dropna())
    con_msft   = set(df_msft["codigo_mgn"].dropna())

    sin_google = todos_pdet - con_google
    sin_msft   = todos_pdet - con_msft
    sin_ambos  = todos_pdet - (con_google | con_msft)

    log.info(f"   Municipios PDET totales          : {len(todos_pdet):,}")
    log.info(f"   Con cobertura Google             : {len(con_google):,}")
    log.info(f"   Con cobertura Microsoft          : {len(con_msft):,}")
    log.info(f"   Sin cobertura Google             : {len(sin_google):,}")
    log.info(f"   Sin cobertura Microsoft          : {len(sin_msft):,}")
    log.info(f"   Sin cobertura en ninguna fuente  : {len(sin_ambos):,}")

    # ── 9. Imprimir resumen en consola ───────────────────────────────────────
    sep = "─" * 60
    print(f"\n{sep}")
    print("  RESUMEN EDA - SEMANA 3")
    print(sep)
    print(f"  {'Colección':<30} {'Documentos':>12}")
    print(f"  {'territorios_pdet':<30} {n_municipios:>12,}")
    print(f"  {'google_buildings':<30} {n_google:>12,}")
    print(f"  {'microsoft_buildings':<30} {n_microsoft:>12,}")
    print(sep)

    if stats_g:
        print(f"\n  GOOGLE BUILDINGS")
        print(f"  {'Municipios cubiertos':<35}: {len(con_google):,} / {len(todos_pdet):,}")
        print(f"  {'Área total estimada (m²)':<35}: {stats_g.get('area_total_m2', 0):,.2f}")
        print(f"  {'Área promedio por edificio (m²)':<35}: {stats_g.get('area_promedio_m2', 0):,.2f}")
        print(f"  {'Confianza promedio':<35}: {stats_g.get('conf_promedio', 0):.4f}")

    if stats_m:
        print(f"\n  MICROSOFT BUILDINGS")
        print(f"  {'Municipios cubiertos':<35}: {len(con_msft):,} / {len(todos_pdet):,}")
        print(f"  {'Altura promedio (m)':<35}: {stats_m.get('altura_promedio_m') or 'N/A'}")
        print(f"  {'Área estimada disponible':<35}: No incluida en dataset")

    print(f"\n  COBERTURA COMPARATIVA")
    print(f"  {'Solo en Google':<35}: {len(con_google - con_msft):,} municipios")
    print(f"  {'Solo en Microsoft':<35}: {len(con_msft - con_google):,} municipios")
    print(f"  {'En ambas fuentes':<35}: {len(con_google & con_msft):,} municipios")
    print(f"  {'Sin cobertura en ninguna':<35}: {len(sin_ambos):,} municipios")
    print(f"\n{sep}")

    print("\n  ARCHIVOS GENERADOS:")
    print("  - eda_reporte_municipios.csv   (todos los municipios, ambas fuentes)")
    print("  - eda_resumen_general.csv      (estadísticas descriptivas globales)")
    print("  - eda_top20_google.csv         (top 20 municipios Google)")
    print("  - eda_top20_microsoft.csv      (top 20 municipios Microsoft)")
    print("  - eda_comparativa.csv          (Google vs Microsoft lado a lado)")
    print(f"{sep}\n")

    cliente.close()
    log.info("EDA completado.")


if __name__ == "__main__":
    main()