import geopandas as gpd
import pandas as pd
import unicodedata
import sys
import os
import matplotlib.pyplot as plt

# =========================
# CONFIGURACIÓN: RUTAS
# =========================
ruta_shp = r"C:\Users\danie\Downloads\MGN2025_00_COLOMBIA\MGN_2025_COLOMBIA\ADMINISTRATIVO\MGN_ADM_MPIO_GRAFICO.shp"
ruta_pdet = r"C:\Users\danie\Downloads\MunicipiosPDET.xlsx"

out_file = "pdet_final.geojson"

# =========================
# UTILIDADES
# =========================
def normalizar_texto(texto):
    if pd.isna(texto):
        return ""
    texto = str(texto).upper().strip()
    return ''.join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )

def normalizar_codigo(valor, largo=None):
    if pd.isna(valor):
        return ""
    valor = str(valor).strip()
    valor = valor.replace(".0", "")
    valor = ''.join(c for c in valor if c.isdigit())

    if largo is not None:
        return valor.zfill(largo)

    return valor

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

# =========================
# SCRIPT PRINCIPAL
# =========================
def main():
    print("Cargando archivos... esto puede tardar un poco debido al tamaño del SHP.")

    if not os.path.exists(ruta_shp):
        print(f"ERROR: no se encuentra el SHP en: {ruta_shp}")
        sys.exit(1)

    if not os.path.exists(ruta_pdet):
        print(f"ERROR: no se encuentra el Excel en: {ruta_pdet}")
        sys.exit(1)

    gdf = gpd.read_file(ruta_shp)
    pdet = pd.read_excel(ruta_pdet)

    # =========================
    # DETECTAR COLUMNAS SHP
    # =========================
    col_mpio = find_column(
        gdf,
        ["mpio_cnmbr", "MPIO_CNMBR", "MPIO_NOMBRE", "NOMBRE_MPIO", "MPIO_NAME"]
    )

    col_dpto = find_column(
        gdf,
        ["dpto_cnmbr", "DPTO_CNMBR", "DPTO_NOMBRE", "NOMBRE_DPTO", "DPTO_NAME"]
    )

    col_codigo_mpio = find_column(
        gdf,
        ["mpio_ccdgo", "MPIO_CCDGO", "MPIO_CODIGO", "COD_MPIO"]
    )

    col_codigo_dpto = find_column(
        gdf,
        ["dpto_ccdgo", "DPTO_CCDGO", "DPTO_CODIGO", "COD_DPTO"]
    )

    # =========================
    # DETECTAR COLUMNAS EXCEL
    # =========================
    col_mpio_pdet = find_column(
        pdet,
        ["Municipio", "MPIO", "NOMBRE_MUNICIPIO", "Nombre Municipio"]
    )

    col_dpto_pdet = find_column(
        pdet,
        ["Departamento", "DPTO", "NOMBRE_DEPARTAMENTO", "Nombre Departamento"]
    )

    col_codigo_pdet = find_column(
        pdet,
        ["Código DANE Municipio", "Código Dane Municipio", "Codigo Dane Municipio", "CODIGO_DANE", "CODIGO", "DANE"]
    )

    if col_mpio is None or col_dpto is None:
        print("ERROR: no se encontraron columnas de municipio/departamento en el SHP.")
        print("Columnas SHP:", list(gdf.columns))
        sys.exit(1)

    if col_codigo_mpio is None or col_codigo_dpto is None:
        print("ERROR: no se encontraron columnas de código departamento/municipio en el SHP.")
        print("Columnas SHP:", list(gdf.columns))
        sys.exit(1)

    if col_mpio_pdet is None or col_dpto_pdet is None or col_codigo_pdet is None:
        print("ERROR: no se encontraron columnas necesarias en el Excel PDET.")
        print("Columnas Excel:", list(pdet.columns))
        sys.exit(1)

    print("\nColumnas detectadas:")
    print(f"  SHP municipio:          {col_mpio}")
    print(f"  SHP departamento:       {col_dpto}")
    print(f"  SHP código dpto:        {col_codigo_dpto}")
    print(f"  SHP código mpio:        {col_codigo_mpio}")
    print(f"  Excel municipio:        {col_mpio_pdet}")
    print(f"  Excel departamento:     {col_dpto_pdet}")
    print(f"  Excel código DANE:      {col_codigo_pdet}")

    # =========================
    # LIMPIEZA DE TEXTOS
    # =========================
    gdf["MPIO_LIMPIO"] = gdf[col_mpio].apply(normalizar_texto)
    gdf["DPTO_LIMPIO"] = gdf[col_dpto].apply(normalizar_texto)

    pdet["MPIO_LIMPIO"] = pdet[col_mpio_pdet].apply(normalizar_texto)
    pdet["DPTO_LIMPIO"] = pdet[col_dpto_pdet].apply(normalizar_texto)

    # =========================
    # CREAR CÓDIGO DANE COMPLETO EN SHP
    # =========================
    gdf["COD_DANE_COMPLETO"] = (
        gdf[col_codigo_dpto].apply(lambda x: normalizar_codigo(x, 2)) +
        gdf[col_codigo_mpio].apply(lambda x: normalizar_codigo(x, 3))
    )

    pdet["COD_DANE_COMPLETO"] = pdet[col_codigo_pdet].apply(lambda x: normalizar_codigo(x, 5))

    print("\nEjemplos de códigos SHP:")
    print(gdf[[col_dpto, col_mpio, "COD_DANE_COMPLETO"]].head(10))

    print("\nEjemplos de códigos PDET:")
    print(pdet[[col_dpto_pdet, col_mpio_pdet, "COD_DANE_COMPLETO"]].head(10))

    # =========================
    # CRUCE POR CÓDIGO DANE COMPLETO
    # =========================
    merged = gdf.merge(
        pdet,
        on="COD_DANE_COMPLETO",
        how="inner"
    )

    print(f"\nCruce por código DANE completo: encontrados {len(merged)} registros.")

    # =========================
    # MOSTRAR FALTANTES
    # =========================
    codigos_encontrados = set(merged["COD_DANE_COMPLETO"].astype(str))

    faltantes = pdet[
        ~pdet["COD_DANE_COMPLETO"].astype(str).isin(codigos_encontrados)
    ]

    if len(faltantes) > 0:
        print("\n⚠️ Municipios PDET que NO cruzaron por código DANE completo:")
        print(faltantes[[col_dpto_pdet, col_mpio_pdet, "COD_DANE_COMPLETO"]])
        print(f"Total faltantes: {len(faltantes)}")

    # =========================
    # RESULTADOS
    # =========================
    print("\n--- RESULTADOS ---")
    print(f"Municipios cargados del SHP: {len(gdf)}")
    print(f"Municipios en tu lista PDET: {len(pdet)}")
    print(f"Municipios PDET encontrados tras el cruce: {len(merged)}")

    if len(merged) == 170:
        print("\n✅ Cruce correcto: se encontraron los 170 municipios PDET.")
    else:
        print(f"\n⚠️ Atención: se esperaban 170 municipios PDET, pero se encontraron {len(merged)}.")

    # =========================
    # EXPORTAR GEOJSON
    # =========================
    if len(merged) > 0:
        cols_to_drop = [
            c for c in ["MPIO_LIMPIO_x", "DPTO_LIMPIO_x", "MPIO_LIMPIO_y", "DPTO_LIMPIO_y"]
            if c in merged.columns
        ]

        resultado_final = merged.drop(columns=cols_to_drop)

        try:
            resultado_final.to_file(out_file, driver="GeoJSON")
            print(f"\n✅ Archivo '{out_file}' creado correctamente.")
        except Exception as e:
            print("Error al exportar GeoJSON:", e)
            sys.exit(1)

        print("Mostrando mapa...")
        ax = resultado_final.plot(figsize=(10, 10), legend=False)
        ax.set_axis_off()
        plt.show()

    else:
        print("\n⚠️ ERROR: No se encontraron coincidencias.")

if __name__ == "__main__":
    main()