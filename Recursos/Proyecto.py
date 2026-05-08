import geopandas as gpd
import pandas as pd
import unicodedata
import sys
import os
import matplotlib.pyplot as plt

# =========================
# CONFIGURACIÓN: RUTAS
# =========================
ruta_shp = r"C:\Users\juanp\OneDrive\Escritorio\Juanp\Administrador de Bases de Datos\MGN2025_00_COLOMBIA\MGN_2025_COLOMBIA\ADMINISTRATIVO\MGN_ADM_MPIO_GRAFICO.shp"
ruta_pdet = r"C:\Users\juanp\OneDrive\Escritorio\Juanp\Administrador de Bases de Datos\MunicipiosPDET.xlsx"

# =========================
# UTILIDADES
# =========================
def normalizar_texto(texto):
    if pd.isna(texto):
        return ""
    texto = str(texto).upper().strip()
    return ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')

def find_column(df, candidates):
    cols = list(df.columns)
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        cl = cand.lower()
        if cl in low:
            return low[cl]
    for cand in candidates:
        cl = cand.lower()
        for c in cols:
            if cl in c.lower() or c.lower() in cl:
                return c
    return None

def main():
    print("Cargando archivos... esto puede tardar un poco debido al tamaño del SHP.")

    if not os.path.exists(ruta_shp):
        print(f"ERROR: no se encuentra el SHP en: {ruta_shp}")
        sys.exit(1)
    if not os.path.exists(ruta_pdet):
        print(f"ERROR: no se encuentra el Excel en: {ruta_pdet}")
        sys.exit(1)

    try:
        gdf = gpd.read_file(ruta_shp)
    except Exception as e:
        print("Error leyendo el SHP:", e)
        sys.exit(1)

    try:
        pdet = pd.read_excel(ruta_pdet)
    except Exception as e:
        print("Error leyendo el Excel:", e)
        sys.exit(1)

    col_mpio = find_column(gdf, ["MPIO_CNMBR", "MPIO_NOMBRE", "NOMBRE_MPIO", "MPIO_NAME"])
    col_dpto = find_column(gdf, ["DPTO_CNMBR", "DPTO_NOMBRE", "NOMBRE_DPTO", "DPTO_NAME"])
    col_codigo = find_column(gdf, ["MPIO_CCDGO", "MPIO_CODIGO", "CODIGO", "COD_MPIO"])

    if col_mpio is None or col_dpto is None:
        print("No se encontraron columnas de nombre de municipio/departamento en el SHP. Columnas disponibles:", list(gdf.columns))
        sys.exit(1)

    gdf["MPIO_LIMPIO"] = gdf[col_mpio].apply(normalizar_texto)
    gdf["DPTO_LIMPIO"] = gdf[col_dpto].apply(normalizar_texto)

    col_mpio_pdet = find_column(pdet, ["Municipio", "MPIO", "NOMBRE_MUNICIPIO"])
    col_dpto_pdet = find_column(pdet, ["Departamento", "DPTO", "NOMBRE_DEPARTAMENTO"])
    col_codigo_pdet = find_column(pdet, ["Código Dane Municipio", "Codigo Dane Municipio", "CODIGO_DANE", "CODIGO"])

    if col_mpio_pdet is None or col_dpto_pdet is None:
        print("No se encontraron las columnas 'Municipio' y 'Departamento' en el Excel. Columnas disponibles:", list(pdet.columns))
        sys.exit(1)

    pdet["MPIO_LIMPIO"] = pdet[col_mpio_pdet].apply(normalizar_texto)
    pdet["DPTO_LIMPIO"] = pdet[col_dpto_pdet].apply(normalizar_texto)

    merged = None
    if col_codigo is not None and col_codigo_pdet is not None:
        gdf[col_codigo] = gdf[col_codigo].astype(str).str.zfill(5)
        pdet[col_codigo_pdet] = pdet[col_codigo_pdet].astype(str).str.zfill(5)
        merged = gdf.merge(pdet, left_on=col_codigo, right_on=col_codigo_pdet, how="inner")
        print(f"Cruce por código DANE: encontrados {len(merged)} registros.")

    if merged is None or len(merged) == 0:
        merged = gdf.merge(pdet, on=["MPIO_LIMPIO", "DPTO_LIMPIO"], how="inner")
        print(f"Cruce por nombre: encontrados {len(merged)} registros.")

    print(f"\n--- RESULTADOS ---")
    print(f"Municipios cargados del SHP: {len(gdf)}")
    print(f"Municipios en tu lista PDET: {len(pdet)}")
    print(f"Municipios PDET encontrados tras el cruce: {len(merged)}")

    if len(merged) > 0:
        cols_to_drop = [c for c in ["MPIO_LIMPIO", "DPTO_LIMPIO"] if c in merged]
        resultado_final = merged.drop(columns=cols_to_drop)

        out_file = "pdet_final.geojson"
        try:
            resultado_final.to_file(out_file, driver="GeoJSON")
            print(f"\n✅ ¡Éxito! Archivo '{out_file}' creado correctamente.")
        except Exception as e:
            print("Error al exportar GeoJSON:", e)

        if "geometry" in resultado_final.columns:
            print("Mostrando mapa (ventana matplotlib)...")
            ax = resultado_final.plot(column=col_mpio, figsize=(10, 10), legend=False)
            ax.set_axis_off()
            plt.show()
    else:
        print("\n⚠️ ERROR: No se encontraron coincidencias. Revisa los nombres/formatos en tu Excel o en el SHP.")

if __name__ == "__main__":
    main()