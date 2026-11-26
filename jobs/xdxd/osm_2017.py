"""Rebuild the Mapzen "imposm" OSM shapefiles XD_XD used (Mapzen Metro Extracts went offline in 2018)
from the OpenStreetMap planet of 2017-05-01 (planet-170501.osm.bz2, AWS open-data mirror of
planet.openstreetmap.org), for each SpaceNet-2 city.

Pipeline (run_osm_2017.sh): lbzip2 -d planet | osmium extract (4 city boxes, one pass)
  -> osmium export (GeoJSON seq: areas assembled into (multi)polygons) -> this script -> shapefiles.

Writes <out>/<name>/<name>_{buildings,landusages,roads,waterareas}.shp with the imposm 'type'
attribute (the OSM tag value), which is all XD_XD's v16.py reads:
  buildings   area polygons (building=*)
  landusages  landuse in {industrial, residential, forest, farmyard} (the only types v16 uses)
  roads       highway=* / railway=* lines (v16 drops type == 'subway')
  waterareas  natural=water, waterway=riverbank, landuse=reservoir|basin polygons
"""
import json
import sys
from pathlib import Path

import fiona
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

CITIES = {  # name used by XD_XD's v16.py -> (W, S, E, N) of the SN2 AOI + ~0.05 deg margin
    "las-vegas_nevada_osm": (-115.36, 36.07, -115.10, 36.31),
    "paris_france_osm": (2.14, 48.93, 2.35, 49.11),
    "shanghai_china_osm": (121.54, 31.15, 121.80, 31.48),
    "ex_s2cCo6gpCXAvihWVygCAfSjNVksnQ_osm": (32.44, 15.46, 32.62, 15.79),  # Khartoum
}
LANDUSE = {"industrial", "residential", "forest", "farmyard"}
WATER_LANDUSE = {"reservoir", "basin"}


def classify(tags, is_area):
    out = []
    lu = tags.get("landuse")
    if is_area:
        if "building" in tags:
            out.append(("buildings", tags["building"]))
        if lu in LANDUSE:
            out.append(("landusages", lu))
        if tags.get("natural") == "water" or tags.get("waterway") == "riverbank" or lu in WATER_LANDUSE:
            out.append(("waterareas", tags.get("natural") or tags.get("waterway") or lu))
    else:
        if "highway" in tags:
            out.append(("roads", tags["highway"]))
        elif "railway" in tags:
            out.append(("roads", tags["railway"]))
    return out


def convert(name, geojsonseq, out_root):
    feats = {k: [] for k in ("buildings", "landusages", "roads", "waterareas")}
    with open(geojsonseq) as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            ft = json.loads(line)
            g = ft.get("geometry")
            if not g:
                continue
            is_area = g["type"] in ("Polygon", "MultiPolygon")
            if not is_area and g["type"] != "LineString":
                continue
            tags = ft.get("properties", {})
            for layer, typ in classify(tags, is_area):
                feats[layer].append((g, typ, tags.get("name", "")))
    d = Path(out_root) / name
    d.mkdir(parents=True, exist_ok=True)
    for layer, items in feats.items():
        geom_type = "LineString" if layer == "roads" else "Polygon"
        schema = {"geometry": geom_type, "properties": {"name": "str", "type": "str"}}
        n = 0
        with fiona.open(d / f"{name}_{layer}.shp", "w", driver="ESRI Shapefile", crs="EPSG:4326",
                        schema=schema, encoding="utf-8") as f:
            for g, typ, nm in items:
                s = shape(g)
                if geom_type == "Polygon":
                    if not s.is_valid:
                        s = s.buffer(0)
                    if not isinstance(s, (Polygon, MultiPolygon)) or s.is_empty:
                        continue
                f.write({"geometry": mapping(s), "properties": {"name": str(nm)[:200], "type": str(typ)}})
                n += 1
        print(name, layer, n, flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "config":  # osmium extract config for all cities in one pass
        print(json.dumps({"extracts": [{"output": f"{nm}.osm.pbf", "bbox": list(bb)} for nm, bb in CITIES.items()]}))
    else:  # convert <name> <geojsonseq> <out_root>
        convert(sys.argv[1], sys.argv[2], sys.argv[3])
