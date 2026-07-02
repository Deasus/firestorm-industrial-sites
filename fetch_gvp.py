#!/usr/bin/env python3
"""Smithsonian Global Volcanism Program (GVP) Holocene volcanoes fetch.

Pulls the GVP-VOTW WFS GeoServer feed as GeoJSON. This is the same catalog
the Smithsonian's Volcanoes of the World web app uses. Public-domain,
~1,350 Holocene volcanoes globally.

Direct volcano.si.edu KML/CSV downloads are behind an anti-bot check that
returns 403 to automated user agents; the WFS GeoServer at
webservices.volcano.si.edu bypasses that entirely and returns clean
GeoJSON. Verified 2026-07-02.

Source: https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows
Typename: GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

WFS_URL = (
    'https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows'
    '?service=WFS'
    '&version=2.0.0'
    '&request=GetFeature'
    '&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes'
    '&outputFormat=application/json'
)
OUT_JSON = os.path.join('data', '_stage_gvp.json')
HTTP_TIMEOUT = 60

# Buffer distance around each volcano summit. Volcano summits (GVP points)
# are NOT the same as active vent locations — vents / lava flows / caldera
# rims can be km from the summit point. 5 km is the FIRESTORM default
# operational buffer (rationale: covers most known vent offsets while not
# suppressing legitimate wildfires on the flanks of volcanic mountains
# in e.g. Cascades, Aleutian, Hawaii — where forests DO burn).
VOLCANO_BUFFER_M = 5000


def fetch() -> dict:
    req = urllib.request.Request(WFS_URL, headers={
        'User-Agent': 'firestorm-industrial-sites/1.0',
        'Accept':     'application/json',
    })
    sys.stderr.write(f'[gvp] fetching {WFS_URL[:80]}...\n')
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode('utf-8', errors='replace')
    fc = json.loads(body)
    if fc.get('type') != 'FeatureCollection':
        sys.exit(f'[gvp] FATAL: expected FeatureCollection, got {fc.get("type")}')
    sys.stderr.write(f'[gvp] received {len(fc.get("features", []))} volcano features\n')
    return fc


def normalize(fc: dict) -> list[dict]:
    """Reduce GVP WFS output to the fields industrial_sites.geojson uses.
    Each volcano becomes one point feature with class=volcano.
    """
    out: list[dict] = []
    for feat in fc.get('features', []):
        geom = feat.get('geometry') or {}
        if geom.get('type') != 'Point':
            continue
        coords = geom.get('coordinates') or []
        if len(coords) < 2:
            continue
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            continue
        props = feat.get('properties') or {}
        # GVP WFS schema uses PascalCase field names verified 2026-07-02.
        vnum = str(props.get('Volcano_Number') or '').strip()
        vname = str(props.get('Volcano_Name') or 'UNKNOWN').strip().upper()[:80]
        country = str(props.get('Country') or '').strip()[:40]
        last_yr = props.get('Last_Eruption_Year')
        vtype = str(props.get('Primary_Volcano_Type') or '').strip()[:40]
        out.append({
            'source_id':          f'gvp:{vnum}',
            'src_type':           'volcano',
            'class':              'volcano',
            'name':               vname,
            'country':            country,
            'volcano_type':       vtype,
            'last_eruption_year': last_yr,
            'lat':                round(lat, 5),
            'lng':                round(lng, 5),
            'buffer_m':           VOLCANO_BUFFER_M,
        })
    return out


def main() -> int:
    fc = fetch()
    rows = normalize(fc)
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = {
        'generated_utc': now_iso,
        'source':        'Smithsonian GVP-VOTW Holocene Volcanoes',
        'source_url':    WFS_URL,
        'count':         len(rows),
        'volcanoes':     rows,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[gvp] wrote {OUT_JSON}: {len(rows)} volcanoes\n')
    if len(rows) < 500:
        sys.stderr.write('[gvp] WARN: fewer than 500 volcanoes — schema may have drifted\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
