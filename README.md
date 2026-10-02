# Mapas de la industria cervecera — municipios de Querétaro y San Juan del Río (Qro.)

Script en Python que genera dos mapas con datos oficiales del **INEGI**:

1. **Empresas productoras de cerveza** (unidades económicas con SCIAN **312120**
   *Elaboración de cerveza*), representadas con un **proxy del tamaño** de la empresa.
2. **Celdas de Voronoi** que cubren los **comercios al por menor de cerveza**
   (SCIAN **461212** *Comercio al por menor de cerveza*) de ambos municipios.

## Cómo ejecutarlo

```bash
pip install -r requirements.txt
python mapas_cerveza_qro.py
```

El script descarga por sí solo los insumos (si no existen) y escribe todo en `salidas/`.

## Fuentes de datos

| Insumo | Fuente | Uso |
|---|---|---|
| DENUE edición **05/2026** (vigencia 20/05/2026) | INEGI, descarga masiva `https://www.inegi.org.mx/contenidos/masiva/denue/denue_00_31-33_csv.zip` (industrias manufactureras) y `.../denue_00_46112-46311_csv.zip` (comercio al por menor, bloque 2 de 4) | Unidades económicas con su clave SCIAN, ubicación y personal ocupado |
| Marco Geoestadístico (MGN), capa *áreas geoestadísticas municipales* | GeoServer oficial del INEGI (WFS): `https://mapas.inegi.org.mx/geoserver/ows`, capa `geografia:pi_mgn_areas_geoestadisticas_municipales`, filtro `cve_ent='22'` | Polígonos municipales para encuadre y recorte |

La DENUE es el directorio de unidades económicas activas construido con base en los
**Censos Económicos**; se publica por bloques de actividad económica, por lo que el
script descarga los dos bloques que contienen las actividades de interés y filtra el
estado de Querétaro (`cve_ent = 22`) y los municipios `22014` (Querétaro) y
`22016` (San Juan del Río).

## Códigos SCIAN (6 dígitos)

| Código | Actividad | Selección del INEGI |
|---|---|---|
| **312120** | Elaboración de cerveza | Productores (mapa 1) |
| **461212** | Comercio al por menor de cerveza | Comercios minoristas (mapa 2) |

Ambos corresponden al SCIAN 2023, la versión que usa la DENUE vigente.

## Proxy del tamaño de las empresas

La DENUE no publica ventas ni producción, pero sí el campo `per_ocu`
(**personal ocupado**), que agrupa a cada unidad económica en rangos. Ese rango se
convierte en el proxy de tamaño:

| `per_ocu` | Orden | Personas de referencia | Tamaño del símbolo |
|---|---|---|---|
| 0 a 5 personas | 1 | 3 | 70 |
| 6 a 10 personas | 2 | 8 | 120 |
| 11 a 30 personas | 3 | 20 | 190 |
| 31 a 50 personas | 4 | 40 | 280 |
| 51 a 100 personas | 5 | 75 | 400 |
| 101 a 250 personas | 6 | 175 | 540 |
| 251 y más personas | 7 | 300 | 700 |

En el **mapa 1** el área del círculo es proporcional a ese rango. En el **mapa 2**
casi todos los comercios minoristas están en el rango más pequeño (0 a 5 personas),
por lo que ahí el indicador de tamaño/mercado que se usa es el **área de la celda de
Voronoi** (km²): celdas pequeñas indican alta densidad de comercios y fuerte
competencia; celdas grandes indican áreas de servicio amplias y poco competition.

## Método Voronoi

1. Se proyectan los puntos al sistema **UTM 14N (EPSG:32614)**, en metros.
2. Se deduplican coordenadas idénticas (la DENUE registra 3 pares de unidades en la
   misma coordenada): **234 comercios → 231 sitios**.
3. Se calcula el diagrama con `shapely.voronoi_polygons(..., extend_to=caja+25 km, ordered=True)`,
   de modo que las celdas del borde quedan acotadas.
4. Cada celda se **recorta a la unión de los dos municipios**, garantizando que el
   teselado cubra exactamente el área de estudio.
5. **Control de cobertura**: la suma de las áreas de las celdas es 1 453.03 km²,
   idéntica al área de la unión de ambos municipios; ninguna celda cruza la frontera
   municipal (los polígonos de Querétaro y San Juan del Río no se tocan en el MGN).

## Resultados (DENUE 05/2026)

| Concepto | Total | Querétaro | San Juan del Río |
|---|---|---|---|
| Productoras de cerveza (312120) | 3 | 2 | 1 |
| Comercios al por menor de cerveza (461212) | 234 | 157 | 77 |
| Celdas de Voronoi | 231 | 154 | 77 |

Productoras: **Cervecería Cuatro Palos** (6–10 personas), **Compañía Cervecera
Hércules** (11–30) y **Cervecería Primus** (31–50).

Área mediana de celda: 0.98 km² (Querétaro 0.74; San Juan del Río 2.24);
distancia mediana al comercio vecino: 0.25 km.

## Salidas

| Archivo | Contenido |
|---|---|
| `salidas/mapa_productores_cerveza.png` | Mapa 1 (figura estática) |
| `salidas/mapa_productores_cerveza.html` | Mapa 1 interactivo (Leaflet/folium, con capas y popups) |
| `salidas/mapa_voronoi_comercios_cerveza.png` | Mapa 2 (figura estática) |
| `salidas/mapa_voronoi_comercios_cerveza.html` | Mapa 2 interactivo (celdas, comercios y control de capas) |
| `salidas/productores_cerveza.csv` | Ficha de las unidades productoras con su proxy de tamaño |
| `salidas/comercios_cerveza.csv` | Ficha de los 234 comercios con área de celda y vecino más cercano |
| `salidas/celdas_voronoi_comercios.csv` | Celdas con su comercio, área (km²) y vecino más cercano |
| `galeria.html` | Página con ambas figuras incrustadas (vista rápida) |

## Notas y limitaciones

* Las unidades se filtran por la **actividad principal** registrada en la DENUE; una
  cervecería que opere además como restaurante se clasifica por su SCIAN principal y
  no necessarily aparecerá como 312120.
* El personal ocupado está en **rangos**, no en valores exactos: sirve para ordenar
  empresas por tamaño, no para medir empleo.
* Los polígonos y las claves provienen del Marco Geoestadístico vigente del INEGI;
  las coordenadas de los establecimientos son las que reporta la DENUE.
