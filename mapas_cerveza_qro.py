#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mapas de la industria cervecera en los municipios de Querétaro y San Juan del Río
Estado de Querétaro, México.

Genera dos mapas a partir de datos oficiales del INEGI:

  1. Unidades económicas PRODUCTORAS DE CERVEZA (SCIAN 312120 "Elaboración de
     cerveza"), representadas con un *proxy* del tamaño de la empresa: los rangos
     de personal ocupado (per_ocu) que publica la DENUE.

  2. Celdas de VORONOI que cubren los COMERCIOS AL POR MENOR DE CERVEZA
     (SCIAN 461212 "Comercio al por menor de cerveza"), recortadas al área de
     los dos municipios.

Fuentes
-------
  * INEGI. Directorio Estadístico Nacional de Unidades Económicas (DENUE),
    edición 05/2026 (vigencia 20/05/2026). Los archivos se publican por
    bloques de actividad económica:
        - Industrias manufactureras : denue_00_31-33_csv.zip
        - Comercio al por menor (2 de 4): denue_00_46112-46311_csv.zip
    Se filtran los registros con cve_ent = 22 (Querétaro) y los municipios
    22014 (Querétaro) y 22016 (San Juan del Río).

  * INEGI. Marco Geoestadístico (MGN), capa "áreas geoestadísticas municipales",
    consultada por WFS al GeoServer oficial del INEGI.

Uso
---
    python mapas_cerveza_qro.py

Salidas (carpeta salidas/)
--------------------------
  mapa_productores_cerveza.png / .html
  mapa_voronoi_comercios_cerveza.png / .html
  productores_cerveza.csv
  comercios_cerveza.csv
  celdas_voronoi_comercios.csv
"""

from __future__ import annotations

import base64
import json
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import STRtree, voronoi_polygons
from shapely.geometry import MultiPoint, Point
from scipy.spatial import cKDTree

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patheffects import withStroke  # noqa: E402

import folium  # noqa: E402
from branca.colormap import LinearColormap  # noqa: E402
from folium.plugins import MeasureControl  # noqa: E402

# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATOS = BASE / "datos"
SALIDAS = BASE / "salidas"

# Denominaciones geoestadísticas del INEGI
ENT = "22"
MUNICIPIOS = {
    "014": "Querétaro",
    "016": "San Juan del Río",
}
CVEGEO = {f"{ENT}{m}" for m in MUNICIPIOS}

# Códigos SCIAN (versión2023, usados por la DENUE vigente)
SCIAN_PRODUCTORES = "312120"  # Elaboración de cerveza
SCIAN_RETAIL = "461212"  # Comercio al por menor de cerveza

URL_DENUE_MANUFACTURA = "https://www.inegi.org.mx/contenidos/masiva/denue/denue_00_31-33_csv.zip"
URL_DENUE_RETAIL = "https://www.inegi.org.mx/contenidos/masiva/denue/denue_00_46112-46311_csv.zip"
URL_MGN = (
    "https://mapas.inegi.org.mx/geoserver/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=geografia:pi_mgn_areas_geoestadisticas_municipales"
    "&CQL_FILTER=cve_ent%3D%2722%27&outputFormat=application/json"
)

ARCHIVO_DENUE_MANUFACTURA = DATOS / "denue_31-33_csv.zip"
ARCHIVO_DENUE_RETAIL = DATOS / "denue_46112-46311_csv.zip"
ARCHIVO_MGN = DATOS / "municipios_qro.geojson"

CRS_TRABAJO = "EPSG:32614"  # UTM zona 14N (metros): corresponde a Querétaro
CRS_GEOGRAFICO = "EPSG:4326"

# Rangos de personal ocupado de la DENUE -> (orden, personas de referencia)
# La DENUE clasifica las unidades económicas por rangos de personal ocupado,
# que es el único indicador de tamaño a nivel de unidad económica.
RANGOS_PER_OCU = {
    "0 a 5 personas": (1, 3),
    "6 a 10 personas": (2, 8),
    "11 a 30 personas": (3, 20),
    "31 a 50 personas": (4, 40),
    "51 a 100 personas": (5, 75),
    "101 a 250 personas": (6, 175),
    "251 y más personas": (7, 300),
    "251 y m\u00e1s personas": (7, 300),
}
ETIQUETAS_RANGO = {
    0: "No especificado",
    1: "0 a 5",
    2: "6 a 10",
    3: "11 a 30",
    4: "31 a 50",
    5: "51 a 100",
    6: "101 a 250",
    7: "251 y más",
}
# Área (en puntos^2) del círculo que representa cada rango
TAMANOS_RANGO = {0: 45, 1: 70, 2: 120, 3: 190, 4: 280, 5: 400, 6: 540, 7: 700}

COLS_DENUE = [
    "id",
    "nom_estab",
    "raz_social",
    "codigo_act",
    "nombre_act",
    "per_ocu",
    "tipoUniEco",
    "nomb_asent",
    "cod_postal",
    "cve_ent",
    "cve_mun",
    "municipio",
    "localidad",
    "latitud",
    "longitud",
    "fecha_alta",
]

FUENTE_DENUE = "INEGI, DENUE edición 05/2026"
FUENTE_MGN = "INEGI, Marco Geoestadístico (MGN)"


# ---------------------------------------------------------------------------
# Utilidades de datos
# ---------------------------------------------------------------------------
def _cabecera(msg: str) -> None:
    print(f"\n=== {msg}")


def corregir_texto(valor):
    """Repara el doble codificado (UTF-8 leído como latin-1) de nombres del MGN."""
    if not isinstance(valor, str):
        return valor
    try:
        return valor.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return valor


def descargar(url: str, destino: Path) -> Path:
    if destino.exists() and destino.stat().st_size > 0:
        print(f"  ya disponible: {destino.name}")
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    print(f"  descargando {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r, open(destino, "wb") as out:
        while True:
            bloque = r.read(1 << 20)
            if not bloque:
                break
            out.write(bloque)
    print(f"  guardado: {destino.name} ({destino.stat().st_size / 1e6:,.1f} MB)")
    return destino


def leer_denue(zip_path: Path, codigo_act: str) -> pd.DataFrame:
    """Extrae de un archivo DENUE las unidades de la entidad 22 con un SCIAN dado."""
    with zipfile.ZipFile(zip_path) as z:
        nombre = next(
            n
            for n in z.namelist()
            if n.endswith(".csv") and "diccionario" not in n and "metadatos" not in n
        )
        partes = []
        with z.open(nombre) as fh:
            for bloque in pd.read_csv(
                fh,
                usecols=COLS_DENUE,
                dtype=str,
                encoding="latin-1",
                chunksize=200_000,
                low_memory=False,
            ):
                sel = (bloque["cve_ent"] == ENT) & (bloque["codigo_act"] == codigo_act)
                if sel.any():
                    partes.append(bloque[sel])
    if not partes:
        return pd.DataFrame(columns=COLS_DENUE)
    out = pd.concat(partes, ignore_index=True)
    for columna in out.columns:  # la DENUE alinea algunos campos con espacios
        out[columna] = out[columna].str.strip()
    return out


def agregar_proxy_tamano(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte el rango de personal ocupado en un proxy numérico de tamaño."""
    claves = {k.strip().lower(): v for k, v in RANGOS_PER_OCU.items()}
    orden, personas = [], []
    for valor in df["per_ocu"].fillna("").str.strip().str.lower():
        o, p = claves.get(valor, (0, np.nan))
        orden.append(o)
        personas.append(p)
    df = df.copy()
    df["per_ocu_orden"] = orden
    df["per_ocu_personas"] = personas
    df["per_ocu_etiqueta"] = [ETIQUETAS_RANGO[o] for o in orden]
    return df


def a_geodataframe(df: pd.DataFrame) -> gpd.GeoDataFrame:
    gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitud"].astype(float), df["latitud"].astype(float)),
        crs=CRS_GEOGRAFICO,
    )
    return gdf.to_crs(CRS_TRABAJO)


def cargar_municipios() -> gpd.GeoDataFrame:
    gdf = gpd.read_file(ARCHIVO_MGN)
    gdf["nomgeo"] = gdf["nomgeo"].map(corregir_texto)
    gdf = gdf[["cvegeo", "cve_mun", "nomgeo", "geometry"]].to_crs(CRS_TRABAJO)
    return gdf


# ---------------------------------------------------------------------------
# Voronoi
# ---------------------------------------------------------------------------
def construir_voronoi(
    puntos: gpd.GeoDataFrame, cobertura, buffer_m: float = 25_000
) -> tuple[gpd.GeoDataFrame, dict]:
    """
    Celdas de Voronoi de los puntos, recortadas al polígono `cobertura`.

    Devuelve (gpd.GeoDataFrame de celdas, dict coordenada -> id de celda).
    """
    # Coordenadas únicas (la DENUE registra algunas unidades con coordenadas iguales)
    unicas: dict[tuple[float, float], Point] = {}
    for pt in puntos.geometry:
        unicas.setdefault((round(pt.x, 2), round(pt.y, 2)), Point(pt.x, pt.y))
    pts = list(unicas.values())

    env = cobertura.envelope.buffer(buffer_m)
    # extend_to define el recuadro que acota las celdas "infinitas" del borde
    polis = list(voronoi_polygons(MultiPoint(pts), extend_to=env, ordered=True).geoms)

    # Cada punto cae en exactamente una celda (su propia celda)
    if len(polis) == len(pts) and all(p.covers(q) for p, q in zip(polis, pts)):
        punto_a_celda = {pt: i for i, pt in enumerate(pts)}
    else:  # respaldo: asignación espacial punto-celda
        tree = STRtree(polis)
        idx_punto, idx_celda = tree.query(np.asarray(pts, dtype=object), predicate="within")
        punto_a_celda = {pts[i]: int(j) for i, j in zip(idx_punto, idx_celda)}

    filas = []
    for j, poli in enumerate(polis):
        recorte = poli.intersection(cobertura)
        if recorte.is_empty:
            continue
        filas.append({"celda_id": j, "geometry": recorte})
    celdas = gpd.GeoDataFrame(filas, geometry="geometry", crs=CRS_TRABAJO)

    coord_a_celda = {}
    for key, pt in unicas.items():
        coord_a_celda[key] = punto_a_celda.get(pt)
    return celdas, coord_a_celda


def preparar_comercios_voronoi(
    comercios: gpd.GeoDataFrame, celdas: gpd.GeoDataFrame, coord_a_celda: dict
) -> gpd.GeoDataFrame:
    """Regresa los comercios con su celda asignada, área de celda y vecino más cercano."""
    celdas_idx = celdas.set_index("celda_id")
    ids, areas = [], []
    for pt in comercios.geometry:
        key = (round(pt.x, 2), round(pt.y, 2))
        cid = coord_a_celda.get(key)
        if cid is None or cid not in celdas_idx.index:
            ids.append(np.nan)
            areas.append(np.nan)
        else:
            ids.append(cid)
            areas.append(celdas_idx.loc[cid, "geometry"].area / 1e6)
    out = comercios.copy()
    out["celda_id"] = ids
    out["area_celda_km2"] = areas

    coords = np.c_[out.geometry.x, out.geometry.y]
    if len(out) > 1:
        dist, _ = cKDTree(coords).query(coords, k=2)
        out["vecino_mas_cercano_km"] = dist[:, 1] / 1000.0
    else:
        out["vecino_mas_cercano_km"] = np.nan
    return out


# ---------------------------------------------------------------------------
# Figuras
# ---------------------------------------------------------------------------
CMAP_AREA = plt.get_cmap("YlOrRd")


def _norm_area(vmin: float, vmax: float) -> LogNorm:
    vmin = max(min(vmin, vmax * 0.01), 1e-4)
    return LogNorm(vmin=vmin, vmax=max(vmax, vmin * 10))


def add_norte(ax, frac_x: float = 0.93, frac_y: float = 0.86) -> None:
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    x = x0 + frac_x * (x1 - x0)
    y = y0 + frac_y * (y1 - y0)
    dx = 0.04 * (x1 - x0)
    dy = 0.10 * (y1 - y0)
    ax.annotate(
        "N",
        xy=(x, y + dy),
        xytext=(x, y),
        ha="center",
        va="center",
        fontsize=11,
        fontweight="bold",
        arrowprops=dict(arrowstyle="-|>", color="black", lw=1.6),
        zorder=12,
    )
    _ = dx


def add_barra_escala(ax, km: float = 5.0, frac_x: float = 0.06, frac_y: float = 0.07) -> None:
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    x = x0 + frac_x * (x1 - x0)
    y = y0 + frac_y * (y1 - y0)
    largo = km * 1000  # el eje está en metros (UTM)
    ax.plot([x, x + largo], [y, y], color="black", lw=3, solid_capstyle="butt", zorder=12)
    ax.plot([x, x], [y - 0.012 * (y1 - y0), y + 0.012 * (y1 - y0)], color="black", lw=2, zorder=12)
    ax.plot(
        [x + largo, x + largo],
        [y - 0.012 * (y1 - y0), y + 0.012 * (y1 - y0)],
        color="black",
        lw=2,
        zorder=12,
    )
    ax.text(
        x + largo / 2,
        y + 0.02 * (y1 - y0),
        f"{km:g} km",
        ha="center",
        va="bottom",
        fontsize=9,
        zorder=12,
        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7),
    )


def _base_municipios(ax, munis, contexto, colorear: bool = True) -> None:
    contexto.plot(ax=ax, color="#f2f2f2", edgecolor="#bdbdbd", linewidth=0.5, zorder=1)
    colores = {"Querétaro": "#dbe9f6", "San Juan del Río": "#fde9d9"}
    for _, fila in munis.iterrows():
        color = colores.get(fila["nomgeo"], "#eeeeee") if colorear else "#ffffff"
        gpd.GeoSeries([fila.geometry], crs=munis.crs).plot(
            ax=ax, color=color, edgecolor="#4d4d4d", linewidth=1.2, zorder=2
        )


def _encuadre(ax, munis, margen: float = 0.06) -> None:
    """Ajusta la vista al área de estudio (más un margen)."""
    xmin, ymin, xmax, ymax = munis.union_all().bounds
    dx, dy = (xmax - xmin) * margen, (ymax - ymin) * margen
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)


def _etiquetar_municipios(ax, munis) -> None:
    for _, fila in munis.iterrows():
        punto = fila.geometry.representative_point()
        ax.text(
            punto.x,
            punto.y,
            fila["nomgeo"],
            ha="center",
            va="center",
            fontsize=11,
            fontweight="bold",
            color="#5a5a5a",
            zorder=8,
            path_effects=[withStroke(linewidth=3, foreground="white")],
        )


def mapa_productores_png(prod: gpd.GeoDataFrame, munis, contexto, salida: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 9))
    _base_municipios(ax, munis, contexto)

    colores_mun = {"Querétaro": "#1f77b4", "San Juan del Río": "#d62728"}
    # Posiciones relativas de las etiquetas para evitar que se encimen
    presets = [
        ((0, 22), "center", "bottom"),
        ((0, -26), "center", "top"),
        ((36, 4), "left", "center"),
        ((-36, 4), "right", "center"),
        ((32, 26), "left", "bottom"),
        ((-32, 26), "right", "bottom"),
    ]
    for i, (_, fila) in enumerate(prod.iterrows()):
        c = fila.geometry
        tam = TAMANOS_RANGO.get(int(fila["per_ocu_orden"]), 60)
        ax.scatter(
            c.x,
            c.y,
            s=tam,
            color=colores_mun.get(fila["municipio"], "#7f7f7f"),
            edgecolor="black",
            linewidth=1.0,
            alpha=0.85,
            zorder=6,
        )
        (dx, dy), ha, va = presets[i % len(presets)]
        ax.annotate(
            f"{fila['nom_estab']}\n{fila['per_ocu_etiqueta']} pers.",
            xy=(c.x, c.y),
            xytext=(dx, dy),
            textcoords="offset points",
            ha=ha,
            va=va,
            fontsize=8.5,
            zorder=7,
            path_effects=[withStroke(linewidth=3, foreground="white")],
        )

    handles = [
        Line2D(
            [],
            [],
            marker="o",
            linestyle="none",
            markerfacecolor=colores_mun[m],
            markeredgecolor="black",
            markersize=8,
            label=m,
        )
        for m in MUNICIPIOS.values()
    ]
    tam_leyendas = sorted({int(v) for v in prod["per_ocu_orden"] if v})
    for o in tam_leyendas:
        handles.append(
            Line2D([], [], marker="o", linestyle="none", markersize=(TAMANOS_RANGO[o] / 4) ** 0.5,
                   markerfacecolor="none", markeredgecolor="black",
                   label=f"{ETIQUETAS_RANGO[o]} personas")
        )
    ax.legend(handles=handles, loc="lower left", frameon=True, fontsize=9, title="Leyenda",
              title_fontsize=10)

    add_barra_escala(ax)
    add_norte(ax)
    _etiquetar_municipios(ax, munis)
    _encuadre(ax, munis)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(
        "Empresas productoras de cerveza (SCIAN 312120)\n"
        "Municipios de Querétaro y San Juan del Río, Qro.",
        fontsize=14,
        fontweight="bold",
        pad=14,
    )
    ax.text(
        0.5,
        -0.03,
        f"n = {len(prod)} unidades económicas · tamaño del símbolo = rango de personal ocupado (proxy de tamaño)\n"
        f"Fuente: {FUENTE_DENUE}; {FUENTE_MGN}. Sistema de coordenadas: UTM 14N (EPSG:32614).",
        transform=ax.transAxes,
        ha="center",
        fontsize=8.5,
        color="#444444",
    )
    fig.tight_layout()
    fig.savefig(salida, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  guardado: {salida.name}")


def mapa_voronoi_png(celdas, comercios, munis, contexto, salida: Path) -> None:
    vmin = float(np.nanmin(celdas["area_km2"]))
    vmax = float(np.nanmax(celdas["area_km2"]))
    norm = _norm_area(vmin, vmax)

    fig, ax = plt.subplots(figsize=(11, 9))
    contexto.plot(ax=ax, color="#f7f7f7", edgecolor="#d9d9d9", linewidth=0.4, zorder=1)

    celdas.plot(
        ax=ax,
        column="area_km2",
        cmap=CMAP_AREA,
        norm=norm,
        edgecolor="white",
        linewidth=0.4,
        zorder=2,
    )

    munis.boundary.plot(ax=ax, color="#4d4d4d", linewidth=1.4, zorder=4)
    ax.scatter(
        comercios.geometry.x,
        comercios.geometry.y,
        s=9,
        color="#111111",
        edgecolor="white",
        linewidth=0.3,
        zorder=5,
    )

    sm = plt.cm.ScalarMappable(cmap=CMAP_AREA, norm=norm)
    sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("Área de la celda de Voronoi (km²)", fontsize=10)

    add_barra_escala(ax, km=5)
    add_norte(ax)
    _etiquetar_municipios(ax, munis)
    _encuadre(ax, munis)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(
        "Comercios al por menor de cerveza (SCIAN 461212) y celdas de Voronoi\n"
        "Municipios de Querétaro y San Juan del Río, Qro.",
        fontsize=14,
        fontweight="bold",
        pad=14,
    )
    n_qro = int((comercios["municipio"] == "Querétaro").sum())
    n_sjr = int((comercios["municipio"] == "San Juan del Río").sum())
    ax.text(
        0.5,
        -0.03,
        f"n = {len(comercios)} comercios (Querétaro: {n_qro} · San Juan del Río: {n_sjr}) · "
        f"cada celda es el área de influencia de un comercio\n"
        f"Fuente: {FUENTE_DENUE}; {FUENTE_MGN}. Voronoi calculado en UTM 14N y recortado a la unión de ambos municipios.",
        transform=ax.transAxes,
        ha="center",
        fontsize=8.5,
        color="#444444",
    )
    fig.tight_layout()
    fig.savefig(salida, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  guardado: {salida.name}")


def crear_galeria() -> Path:
    """Página con ambas figuras incrustadas (base64) para visualización rápida."""
    def _b64(ruta: Path) -> str:
        return base64.b64encode(ruta.read_bytes()).decode()

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Mapas cerveza: Querétaro y San Juan del Río</title>
<style>
  body {{ font-family: sans-serif; margin: 20px; background: #fafafa; }}
  h1 {{ font-size: 20px; }}
  figure {{ margin: 0 0 24px; }}
  img {{ max-width: 100%; border: 1px solid #ccc; background: #fff; }}
  figcaption {{ font-size: 13px; color: #444; margin-top: 6px; }}
</style>
</head>
<body>
<h1>Mapas de la industria cervecera — Querétaro y San Juan del Río, Qro.</h1>
<figure>
  <img alt="Productoras de cerveza" src="data:image/png;base64,{_b64(SALIDAS / 'mapa_productores_cerveza.png')}">
  <figcaption>1) Unidades productoras de cerveza (SCIAN 312120). El tamaño del símbolo es el rango de
  personal ocupado de la DENUE (proxy del tamaño de la empresa).</figcaption>
</figure>
<figure>
  <img alt="Voronoi de comercios de cerveza" src="data:image/png;base64,{_b64(SALIDAS / 'mapa_voronoi_comercios_cerveza.png')}">
  <figcaption>2) Comercios al por menor de cerveza (SCIAN 461212) y celdas de Voronoi recortadas a la
  unión de los municipios de Querétaro y San Juan del Río.</figcaption>
</figure>
</body>
</html>
"""
    destino = BASE / "galeria.html"
    destino.write_text(html, encoding="utf-8")
    print(f"  guardado: {destino.name}")
    return destino


# ---------------------------------------------------------------------------
# Mapas interactivos (folium)
# ---------------------------------------------------------------------------
def _hex(rgba) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(255 * c) for c in rgba[:3]))


def mapa_productores_html(prod, munis_geo, contexto_geo, salida: Path) -> None:
    centro = [munis_geo.union_all().centroid.y, munis_geo.union_all().centroid.x]
    m = folium.Map(location=centro, zoom_start=11, tiles="OpenStreetMap", control_scale=True)
    folium.GeoJson(
        json.loads(contexto_geo.to_json()),
        name="Municipios (contexto)",
        style_function=lambda _: {"color": "#bdbdbd", "weight": 0.7, "fillOpacity": 0},
        show=False,
    ).add_to(m)
    folium.GeoJson(
        json.loads(munis_geo.to_json()),
        name="Municipios de estudio",
        tooltip=folium.GeoJsonTooltip(fields=["nomgeo"]),
        style_function=lambda _: {"color": "#4d4d4d", "weight": 1.5, "fillOpacity": 0.15},
    ).add_to(m)

    colores = {"Querétaro": "#1f77b4", "San Juan del Río": "#d62728"}
    for _, f in prod.iterrows():
        popup = (
            f"<b>{f['nom_estab']}</b><br>"
            f"Razón social: {f['raz_social'] if isinstance(f['raz_social'], str) else '—'}<br>"
            f"Municipio: {f['municipio']} · Localidad: {f['localidad']}<br>"
            f"SCIAN: {f['codigo_act']} — {f['nombre_act']}<br>"
            f"Personal ocupado: {f['per_ocu']}<br>"
            f"Alta en DENUE: {f['fecha_alta']}"
        )
        folium.CircleMarker(
            location=[f.geometry.y, f.geometry.x],
            radius=4 + 3.5 * int(f["per_ocu_orden"]),
            color="black",
            weight=1,
            fill=True,
            fill_color=colores.get(f["municipio"], "#7f7f7f"),
            fill_opacity=0.85,
            popup=folium.Popup(popup, max_width=320),
            tooltip=f"{f['nom_estab']} · {f['per_ocu_etiqueta']} pers.",
        ).add_to(m)

    b = munis_geo.total_bounds
    m.fit_bounds([[b[1], b[0]], [b[3], b[2]]], padding=(30, 30))
    folium.LayerControl(collapsed=False).add_to(m)
    MeasureControl(position="topleft").add_to(m)
    m.get_root().html.add_child(
        folium.Element(
            "<h4 style='margin:6px 10px; font-family:sans-serif;'>"
            "Productoras de cerveza (SCIAN 312120) — Querétaro y San Juan del Río<br>"
            "<span style='font-size:12px; font-weight:normal;'>Tamaño del círculo = rango de personal ocupado "
            f"({FUENTE_DENUE})</span></h4>"
        )
    )
    m.save(salida)
    print(f"  guardado: {salida.name}")


def mapa_voronoi_html(celdas_geo, comercios_geo, munis_geo, salida: Path) -> None:
    centro = [munis_geo.union_all().centroid.y, munis_geo.union_all().centroid.x]
    m = folium.Map(location=centro, zoom_start=11, tiles="OpenStreetMap", control_scale=True)

    vmin = float(celdas_geo["area_km2"].min())
    vmax = float(celdas_geo["area_km2"].max())
    norm = _norm_area(vmin, vmax)
    cmap = LinearColormap(
        colors=[_hex(CMAP_AREA(t)) for t in np.linspace(0.05, 1.0, 6)],
        vmin=norm.vmin,
        vmax=norm.vmax,
        caption="Área de la celda de Voronoi (km², escala log)",
    )
    cmap.add_to(m)

    folium.GeoJson(
        json.loads(celdas_geo.to_json()),
        name="Celdas de Voronoi",
        style_function=lambda f: {
            "fillColor": _hex(CMAP_AREA(norm(max(f["properties"]["area_km2"], norm.vmin)))),
            "color": "white",
            "weight": 0.6,
            "fillOpacity": 0.65,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["nom_estab", "municipio", "area_km2", "vecino_mas_cercano_km"],
            aliases=["Comercio", "Municipio", "Área celda (km²)", "Vecino más cercano (km)"],
            localize=True,
            labels=True,
        ),
    ).add_to(m)

    folium.GeoJson(
        json.loads(munis_geo.to_json()),
        name="Municipios",
        style_function=lambda _: {"color": "#333333", "weight": 1.6, "fillOpacity": 0},
    ).add_to(m)

    folium.GeoJson(
        json.loads(comercios_geo.to_json()),
        name="Comercios (SCIAN 461212)",
        marker=folium.CircleMarker(radius=3, color="#111111", weight=0.6,
                                   fill=True, fill_color="#111111", fill_opacity=0.9),
        tooltip=folium.GeoJsonTooltip(fields=["nom_estab", "municipio"]),
        show=True,
    ).add_to(m)

    b = munis_geo.total_bounds
    m.fit_bounds([[b[1], b[0]], [b[3], b[2]]], padding=(30, 30))
    folium.LayerControl(collapsed=False).add_to(m)
    MeasureControl(position="topleft").add_to(m)
    m.get_root().html.add_child(
        folium.Element(
            "<h4 style='margin:6px 10px; font-family:sans-serif;'>"
            "Comercios al por menor de cerveza (SCIAN 461212) y celdas de Voronoi — "
            "Querétaro y San Juan del Río<br>"
            "<span style='font-size:12px; font-weight:normal;'>Cada celda es el área de influencia "
            "del comercio; recortada a la unión de ambos municipios</span></h4>"
        )
    )
    m.save(salida)
    print(f"  guardado: {salida.name}")


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main() -> None:
    SALIDAS.mkdir(exist_ok=True)

    _cabecera("Descarga de datos (INEGI)")
    descargar(URL_DENUE_MANUFACTURA, ARCHIVO_DENUE_MANUFACTURA)
    descargar(URL_DENUE_RETAIL, ARCHIVO_DENUE_RETAIL)
    descargar(URL_MGN, ARCHIVO_MGN)

    _cabecera("Municipios")
    munis_todos = cargar_municipios()
    munis = munis_todos[munis_todos["cvegeo"].isin(CVEGEO)].copy()
    contexto = munis_todos[~munis_todos["cvegeo"].isin(CVEGEO)].copy()
    print(f"  {len(munis_todos)} municipios en el estado; de estudio: "
          + ", ".join(sorted(munis["nomgeo"])))
    cobertura = munis.union_all()

    _cabecera(f"Productoras de cerveza — SCIAN {SCIAN_PRODUCTORES}")
    prod_df = agregar_proxy_tamano(leer_denue(ARCHIVO_DENUE_MANUFACTURA, SCIAN_PRODUCTORES))
    prod_df = prod_df[prod_df["cve_mun"].isin(MUNICIPIOS)].reset_index(drop=True)
    prod = a_geodataframe(prod_df)
    print(f"  unidades económicas: {len(prod)}")
    if len(prod):
        print(
            prod.groupby(["municipio", "per_ocu_etiqueta"]).size().rename("n").to_string()
        )

    _cabecera(f"Comercios al por menor de cerveza — SCIAN {SCIAN_RETAIL}")
    ret_df = agregar_proxy_tamano(leer_denue(ARCHIVO_DENUE_RETAIL, SCIAN_RETAIL))
    ret_df = ret_df[ret_df["cve_mun"].isin(MUNICIPIOS)].reset_index(drop=True)
    comercios = a_geodataframe(ret_df)
    print(f"  unidades económicas: {len(comercios)}")
    print(comercios.groupby("municipio").size().rename("n").to_string())

    _cabecera("Celdas de Voronoi")
    celdas, coord_a_celda = construir_voronoi(comercios, cobertura)
    comercios = preparar_comercios_voronoi(comercios, celdas, coord_a_celda)
    celdas = celdas.merge(
        comercios[["celda_id", "nom_estab", "municipio", "vecino_mas_cercano_km"]]
        .dropna(subset=["celda_id"])
        .drop_duplicates("celda_id"),
        on="celda_id",
        how="left",
    )
    celdas["area_km2"] = celdas.geometry.area / 1e6
    area_cobertura = cobertura.area / 1e6
    print(f"  celdas construidas: {len(celdas)} · área total {celdas['area_km2'].sum():,.2f} km²")
    print(f"  área de la unión de ambos municipios: {area_cobertura:,.2f} km² (control de cobertura)")
    print(f"  comercios sin celda (fuera del área): {int(comercios['celda_id'].isna().sum())}")
    print(
        f"  área mediana: {celdas['area_km2'].median():.3f} km² · mín {celdas['area_km2'].min():.3f}"
        f" · máx {celdas['area_km2'].max():.3f}"
    )

    _cabecera("Tablas (CSV)")
    cols_prod = [
        "id", "nom_estab", "raz_social", "codigo_act", "nombre_act", "per_ocu",
        "per_ocu_etiqueta", "per_ocu_personas", "municipio", "localidad",
        "nomb_asent", "cod_postal", "latitud", "longitud", "fecha_alta",
    ]
    prod[cols_prod].to_csv(SALIDAS / "productores_cerveza.csv", index=False, encoding="utf-8-sig")
    print("  guardado: productores_cerveza.csv")

    cols_ret = [
        "id", "nom_estab", "codigo_act", "nombre_act", "per_ocu", "municipio",
        "localidad", "latitud", "longitud", "area_celda_km2",
        "vecino_mas_cercano_km", "fecha_alta",
    ]
    comercios[cols_ret].to_csv(
        SALIDAS / "comercios_cerveza.csv", index=False, encoding="utf-8-sig"
    )
    print("  guardado: comercios_cerveza.csv")

    cols_cel = ["celda_id", "nom_estab", "municipio", "area_km2", "vecino_mas_cercano_km"]
    celdas[cols_cel].to_csv(
        SALIDAS / "celdas_voronoi_comercios.csv", index=False, encoding="utf-8-sig"
    )
    print("  guardado: celdas_voronoi_comercios.csv")

    _cabecera("Mapas (PNG)")
    mapa_productores_png(prod, munis, contexto, SALIDAS / "mapa_productores_cerveza.png")
    mapa_voronoi_png(celdas, comercios, munis, contexto, SALIDAS / "mapa_voronoi_comercios_cerveza.png")
    crear_galeria()

    _cabecera("Mapas interactivos (HTML)")
    prod_ll = prod.to_crs(CRS_GEOGRAFICO)
    com_ll = comercios.to_crs(CRS_GEOGRAFICO)
    celdas_ll = celdas.to_crs(CRS_GEOGRAFICO)
    munis_ll = munis.to_crs(CRS_GEOGRAFICO)
    contexto_ll = contexto.to_crs(CRS_GEOGRAFICO)

    mapa_productores_html(prod_ll, munis_ll, contexto_ll, SALIDAS / "mapa_productores_cerveza.html")
    mapa_voronoi_html(celdas_ll, com_ll, munis_ll, SALIDAS / "mapa_voronoi_comercios_cerveza.html")

    _cabecera("Resumen")
    print(f"  productoras de cerveza (SCIAN {SCIAN_PRODUCTORES}): {len(prod)}")
    print(f"  comercios al por menor de cerveza (SCIAN {SCIAN_RETAIL}): {len(comercios)}")
    print(f"  celdas de Voronoi: {len(celdas)}")
    print(f"  salidas en: {SALIDAS}")


if __name__ == "__main__":
    main()
