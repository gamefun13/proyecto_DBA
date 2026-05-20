# Preparación de la base de datos

## 1. Levantar MongoDB

Abre la terminal en Visual Studio Code y ejecuta:

```powershell
docker compose up -d
```

Después abre el contenedor en Docker y entra a la opción de ejecución (`exec`). Allí ejecuta:

```bash
mongosh
use proyecto
```

El comando `use proyecto` crea la base de datos llamada `proyecto` si todavía no existe.

## 2. Cargar los datos

Ejecuta los scripts en este orden:

1. `01_cargar_pdet_mongodb.py`
	- Cambia la ruta de `GEOJSON_PATH` para que apunte a `pdet_final.geojson`.
2. `script.py`
	- Verifica que los archivos `8e7_buildings.csv` y los demás CSV de edificios estén en la misma carpeta.
3. `microsoftB.py`
	- Está dentro de la carpeta `microsoft_geojson`.

Con los pasos 2 y 3, los datos quedan guardados en MongoDB.

## 3. Ejecutar el análisis de rooftops

Ve a la carpeta `proyecto_DBA/Recursos` y ejecuta:

```powershell
python 02_rooftop_analysis.py --mongo mongodb://localhost:27017/ --out outputs
```
