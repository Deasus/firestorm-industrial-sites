#!/usr/bin/env python3
"""Natural Earth 10m land polygon fetch — used as an offshore-detection proxy.

VIIRS/MODIS false positives over ocean/lake water are dominated by:
  - Sun glint (specular reflection off water surface at low sun angle)
  - Ship stacks (large cargo/naval vessels with hot exhaust)
  - Offshore gas flare platforms (Gulf of Mexico, North Sea, etc.)

None of these are wildfires. Suppressing them requires knowing whether a
detection point sits over land or water — for which the canonical
open-data polygon is Natural Earth 10m land (~1:10M scale, sufficient
resolution for VIIRS' 375m native pixel + 125m geolocation error).

Source: Natural Earth via CDN (S3 backing).
"""
import json
import os
import sys
import urllib.request
import zipfile
import io
import subprocess
from datetime import datetime, timezone

# Two candidate URLs. Try in order.
URLS = [
    'https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip',
    'https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip',
]
HTTP_TIMEOUT = 90
OUT_GEOJSON = os.path.join('data', 'land_10m.geojson')
CACHE_ZIP = os.path.join('data', '_cache_ne_10m_land.zip')


def download() -> str:
    """Return path to cached shapefile zip. Refreshes if >90d old.
    Natural Earth is stable — quarterly refresh is defensive."""
    if os.path.exists(CACHE_ZIP):
        age_s = (datetime.now(tz=timezone.utc).timestamp()
                 - os.path.getmtime(CACHE_ZIP))
        if age_s < 90 * 86400:
            sys.stderr.write(f'[ne] cache hit ({age_s/86400:.0f}d old)\n')
            return CACHE_ZIP

    os.makedirs(os.path.dirname(CACHE_ZIP), exist_ok=True)
    last_err = None
    for url in URLS:
        try:
            sys.stderr.write(f'[ne] downloading {url}...\n')
            req = urllib.request.Request(url, headers={
                'User-Agent': 'firestorm-industrial-sites/1.0'})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read()
            tmp = CACHE_ZIP + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, CACHE_ZIP)
            sys.stderr.write(f'[ne] downloaded {len(data)/1e6:.1f} MB\n')
            return CACHE_ZIP
        except Exception as e:
            last_err = e
            sys.stderr.write(f'[ne] {url} failed: {e}\n')
    sys.exit(f'[ne] FATAL: all URLs failed. Last error: {last_err}')


def shapefile_to_geojson(zip_path: str) -> dict:
    """Convert the Natural Earth land shapefile to GeoJSON.
    Uses ogr2ogr if available (fast, correct), else pyshp fallback.
    """
    # Prefer ogr2ogr if the runner has GDAL — output is authoritative
    # and multi-part polygons stay intact.
    try:
        subprocess.run(['ogr2ogr', '--version'], check=True,
                       capture_output=True, timeout=5)
        return _via_ogr2ogr(zip_path)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        sys.stderr.write('[ne] ogr2ogr not available, falling back to pyshp\n')
        return _via_pyshp(zip_path)


def _via_ogr2ogr(zip_path: str) -> dict:
    """Convert via GDAL's ogr2ogr, reading directly from the zip."""
    # ogr2ogr can read /vsizip/ virtual paths without unpacking
    out_tmp = os.path.join('data', '_ne_ogr_tmp.geojson')
    if os.path.exists(out_tmp):
        os.remove(out_tmp)
    # Find the .shp inside the zip
    with zipfile.ZipFile(zip_path) as zf:
        shp_name = next((n for n in zf.namelist() if n.endswith('.shp')), None)
    if not shp_name:
        sys.exit('[ne] no .shp in zip')
    vsizip_path = f'/vsizip/{os.path.abspath(zip_path)}/{shp_name}'
    subprocess.run(
        ['ogr2ogr', '-f', 'GeoJSON', out_tmp, vsizip_path,
         '-lco', 'COORDINATE_PRECISION=3'],
        check=True, capture_output=True, timeout=120,
    )
    with open(out_tmp) as f:
        fc = json.load(f)
    os.remove(out_tmp)
    sys.stderr.write(f'[ne] ogr2ogr produced {len(fc.get("features", []))} land polygons\n')
    return fc


def _via_pyshp(zip_path: str) -> dict:
    """Fallback: pyshp + shapely. Slower but requirements.txt-installable."""
    import shapefile
    features = []
    with zipfile.ZipFile(zip_path) as zf:
        shp_name = next((n for n in zf.namelist() if n.endswith('.shp')), None)
        shx_name = next((n for n in zf.namelist() if n.endswith('.shx')), None)
        dbf_name = next((n for n in zf.namelist() if n.endswith('.dbf')), None)
        if not (shp_name and shx_name and dbf_name):
            sys.exit('[ne] shapefile components missing from zip')
        shp = io.BytesIO(zf.read(shp_name))
        shx = io.BytesIO(zf.read(shx_name))
        dbf = io.BytesIO(zf.read(dbf_name))
    r = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
    for shp_rec in r.iterShapeRecords():
        g = shp_rec.shape.__geo_interface__
        # Round coordinates for smaller file (3 dp ~ 100m at equator, plenty
        # for a land/water test at VIIRS 375m resolution)
        g = _round_geom(g, 3)
        features.append({'type': 'Feature', 'geometry': g, 'properties': {}})
    sys.stderr.write(f'[ne] pyshp produced {len(features)} land polygons\n')
    return {'type': 'FeatureCollection', 'features': features}


def _round_geom(g: dict, ndigits: int) -> dict:
    """Round all coordinates in a GeoJSON geometry to ndigits."""
    t = g.get('type')
    def r_coords(coords):
        return [round(float(c), ndigits) for c in coords]
    def r_ring(ring):
        return [r_coords(pt) for pt in ring]
    def r_poly(poly):
        return [r_ring(ring) for ring in poly]
    if t == 'Point':
        return {'type': t, 'coordinates': r_coords(g['coordinates'])}
    # GeoJSON coord shapes (per RFC 7946):
    #   Point             coords = [x,y]
    #   MultiPoint / Line coords = [[x,y], ...]
    #   MultiLineString   coords = [[[x,y], ...], ...]              -> ring-of-rings
    #   Polygon           coords = [outer_ring, hole1, hole2, ...]  -> ring-of-rings (SAME SHAPE as MultiLineString)
    #   MultiPolygon      coords = [polygon1, polygon2, ...]        -> [ring-of-rings, ...]
    if t in ('MultiPoint', 'LineString'):
        return {'type': t, 'coordinates': [r_coords(c) for c in g['coordinates']]}
    if t in ('MultiLineString', 'Polygon'):
        return {'type': t, 'coordinates': r_poly(g['coordinates'])}
    if t == 'MultiPolygon':
        return {'type': t, 'coordinates': [r_poly(poly) for poly in g['coordinates']]}
    return g   # unknown geometry type, leave as-is


def main() -> int:
    zip_path = download()
    fc = shapefile_to_geojson(zip_path)
    if not fc.get('features'):
        sys.exit('[ne] FATAL: empty FeatureCollection')

    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    fc['metadata'] = {
        'generated_utc': now_iso,
        'source':        'Natural Earth 10m Land (naturalearthdata.com)',
        'license':       'Public domain',
        'purpose':       'FIRESTORM offshore-detection proxy for VIIRS/MODIS deconfliction',
    }
    os.makedirs(os.path.dirname(OUT_GEOJSON), exist_ok=True)
    tmp = OUT_GEOJSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(fc, f, separators=(',', ':'))
    os.replace(tmp, OUT_GEOJSON)
    size_mb = os.path.getsize(OUT_GEOJSON) / 1e6
    sys.stderr.write(f'[ne] wrote {OUT_GEOJSON}: {len(fc["features"])} polygons, {size_mb:.1f} MB\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
