import argparse
import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import mapping
from pymongo import MongoClient, GEOSPHERE


def read_codes(codes_str, codes_file):
    codes = set()
    if codes_str:
        # Limpia espacios y comas si lo pasas por terminal
        for c in codes_str.replace(',', ' ').split():
            codes.add(c.strip().zfill(5))
    if codes_file:
        p = Path(codes_file)
        if p.exists():
            # Lee el archivo, reemplaza comas por espacios y separa
            content = p.read_text(encoding='utf-8').replace(',', ' ')
            for s in content.split():
                if s.strip():
                    codes.add(s.strip().zfill(5))
    return codes


def geom_to_geojson(geom):
    return mapping(geom)


def main():
    parser = argparse.ArgumentParser(description='Cargar municipios PDET a MongoDB')
    parser.add_argument('--shapefile', required=True,
                        help='Ruta al archivo .shp (ej: C:\\...\\MGN_ADM_MPIO_GRAFICO.shp)')
    parser.add_argument('--codes', help='Lista de códigos MPIO_CCDGO separados por coma')
    parser.add_argument('--codes-file', help='Archivo de texto con códigos (uno por línea)')
    parser.add_argument('--mongo-uri', default='mongodb://localhost:27017',
                        help='URI de conexión a MongoDB')
    parser.add_argument('--db', default='UPME_Solar_Project')
    parser.add_argument('--collection', default='pdet_territories')
    parser.add_argument('--batch', type=int, default=1000, help='Tamaño de lote para inserciones')
    args = parser.parse_args()

    codes = read_codes(args.codes, args.codes_file)
    if not codes:
        print('No se proporcionaron códigos. Proporcione --codes o --codes-file con los 170 códigos PDET.')
        return

    shp_path = Path(args.shapefile)
    if not shp_path.exists():
        print(f'Archivo shapefile no encontrado: {shp_path}')
        return

    print('Leyendo shapefile...')
    gdf = gpd.read_file(str(shp_path))

    print('Asegurando proyección EPSG:4326...')
    try:
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
    except Exception:
        gdf = gdf.to_crs(epsg=4326)

    print('Filtrando por códigos mpio_cdpmp...')

    # Usamos mpio_cdpmp que es el código DIVIPOLA real
    columna_real = 'mpio_cdpmp'

    if columna_real not in gdf.columns:
        print(f'Error: La columna {columna_real} no existe. Columnas actuales: {gdf.columns}')
        return

    # Normalizamos a 5 caracteres por si acaso
    gdf['mpio_cdpmp_str'] = gdf[columna_real].astype(str).str.strip().str.zfill(5)

    # Aplicamos el filtro con los códigos que cargaste del TXT
    filtered = gdf[gdf['mpio_cdpmp_str'].isin(codes)].copy()

    print(f'Elementos filtrados: {len(filtered)}')
    if len(filtered) == 0:
        print('Ningún municipio coincide con los códigos proporcionados.')
        return

    print('Conectando a MongoDB...')
    client = MongoClient(args.mongo_uri)
    db = client[args.db]
    coll = db[args.collection]

    print('Limpiando datos previos en la colección...')
    coll.delete_many({})

    print('Creando índice espacial 2dsphere en el campo "geometry"...')
    coll.create_index([('geometry', GEOSPHERE)])

    docs = []
    for idx, row in filtered.iterrows():
        props = row.drop(labels='geometry').to_dict()
        # Normalizar tipos (numpy -> python)
        for k, v in list(props.items()):
            try:
                json.dumps(v)
            except Exception:
                props[k] = str(v)

        geom = geom_to_geojson(row.geometry)
        doc = {
            'MPIO_CCDGO': row.get('mpio_cdpmp_str') or props.get('MPIO_CCDGO') or props.get(columna_real),
            'properties': props,
            'geometry': geom
        }
        docs.append(doc)

        if len(docs) >= args.batch:
            coll.insert_many(docs)
            docs = []

    if docs:
        coll.insert_many(docs)

    print('Inserción completada.')
    total = coll.count_documents({})
    print(f'Documentos en la colección {args.collection}: {total}')


if __name__ == '__main__':
    main()
