import geopandas as gpd
p = r'C:\Users\danie\Downloads\MGN2025_00_COLOMBIA\MGN_2025_COLOMBIA\ADMINISTRATIVO\MGN_ADM_MPIO_GRAFICO.shp'
g = gpd.read_file(p)
print('Columnas:', list(g.columns))
col = next((c for c in g.columns if c.lower() == 'mpio_ccdgo'), None)
print('Columna detectada:', col)
if col:
    vals = sorted({str(x).strip().zfill(5) for x in g[col].unique()})
    print('Valores ejemplo (5 primeros):', vals[:5])