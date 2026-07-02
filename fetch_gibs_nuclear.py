#!/usr/bin/env python3
"""NASA GIBS Nuclear_Power_Plant_Locations fetch.

GIBS (Global Imagery Browse Services, gibs.earthdata.nasa.gov) is NASA's
public WMTS. It exposes a static point layer of ~235 global nuclear power
plants (SEDAC-sourced, verified 2026-07-02) — one of the very few NASA-
blessed authoritative infrastructure overlays in a machine-readable OGC
format. Public-domain, no license, no auth.

Nuclear plants trigger VIIRS thermal detections routinely — cooling
towers + steam plumes + occasional reactor-hall roof heat — so having a
NASA-verified overlay is a cheap high-value add to our deconfliction
stack. Coverage is global (unlike EPA FRS which is US-only), and the
provenance is "NASA GIBS" not "US EPA", which matters for operators
using FIRESTORM on international mutual-aid or arctic ops.

Method: fetch a single Mapbox Vector Tile at z=0 (whole world), extract
the layer's features. Each feature carries Latitude/Longitude directly
in its properties (no need to decode MVT tile-space coordinates), plus
plant name, country, reactor count, and 30/75/150 km population totals.

Source:  https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/Nuclear_Power_Plant_Locations/...
Docs:    https://gibs.earthdata.nasa.gov/vector-metadata/v1.0/Nuclear_Power_Plant_Locations.json
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

MVT_URL = 'https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/Nuclear_Power_Plant_Locations/default/2km/0/0/0.mvt'
# The z=0 tile in EPSG:4326/2km covers the whole world in ONE tile at this
# TileMatrixSet (verified live 2026-07-02, 235 features returned). No need
# to walk a tile grid.
OUT_JSON = os.path.join('data', '_stage_gibs_nuclear.json')
HTTP_TIMEOUT = 30

# Nuclear plants: cooling towers emit persistent thermal signatures visible
# to VIIRS/MODIS. Buffer 500m matches our EPA-FRS industrial default —
# nuclear plants aren't larger than the reactor complex, and their thermal
# footprint is bounded (cooling towers + steam plume, not a mile-wide
# refinery). If FIRMS sees a hot pixel within 500m it's the plant.
NUCLEAR_BUFFER_M = 500


def fetch_mvt() -> bytes:
    req = urllib.request.Request(MVT_URL, headers={
        'User-Agent': 'firestorm-industrial-sites/1.0',
        'Accept':     'application/vnd.mapbox-vector-tile,application/octet-stream,*/*',
    })
    sys.stderr.write(f'[gibs] fetching {MVT_URL}\n')
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def decode(mvt_bytes: bytes) -> list[dict]:
    """Decode the MVT and return one dict per nuclear plant."""
    try:
        from mapbox_vector_tile import decode as _decode
    except ImportError:
        sys.exit('[gibs] FATAL: mapbox-vector-tile not installed (add to requirements.txt)')

    tile = _decode(mvt_bytes)
    if not tile:
        sys.exit('[gibs] FATAL: empty MVT decode')

    out: list[dict] = []
    for layer_name, layer in tile.items():
        # The GIBS layer name will be Nuclear_Power_Plant_Locations_v1_STD or similar
        for feat in layer.get('features', []):
            props = feat.get('properties') or {}
            # Coordinates live in properties (NASA/SEDAC published them directly);
            # this avoids MVT tile-space decoding and is authoritative.
            try:
                lat = float(props.get('Latitude'))
                lng = float(props.get('Longitude'))
            except (TypeError, ValueError):
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                continue
            plant = str(props.get('Plant') or 'UNKNOWN').strip().upper()[:80]
            country = str(props.get('Country') or '').strip()[:40]
            try:
                num_reactors = int(props.get('NumReactor') or 0)
            except (TypeError, ValueError):
                num_reactors = 0
            out.append({
                'source_id':    f'gibs_npp:{plant.replace(" ","_")}_{country[:3]}',
                'src_type':     'nuclear_plant',
                'class':        'nuclear',
                'name':         plant,
                'country':      country,
                'num_reactors': num_reactors,
                'lat':          round(lat, 5),
                'lng':          round(lng, 5),
                'buffer_m':     NUCLEAR_BUFFER_M,
            })
    return out


def main() -> int:
    mvt = fetch_mvt()
    sys.stderr.write(f'[gibs] fetched {len(mvt)} bytes MVT\n')
    rows = decode(mvt)
    sys.stderr.write(f'[gibs] decoded {len(rows)} nuclear plants\n')

    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = {
        'generated_utc': now_iso,
        'source':        'NASA GIBS Nuclear_Power_Plant_Locations (SEDAC-sourced)',
        'source_url':    MVT_URL,
        'count':         len(rows),
        'plants':        rows,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[gibs] wrote {OUT_JSON}\n')

    if len(rows) < 100:
        sys.stderr.write('[gibs] WARN: fewer than 100 plants — layer may have changed\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
