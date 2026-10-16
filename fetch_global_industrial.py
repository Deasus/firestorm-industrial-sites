#!/usr/bin/env python3
"""Global industrial heat sources for VIIRS/MODIS false-positive deconfliction.

EPA FRS (fetch_epa_frs.py) only covers the United States. Once the FIRMS pipeline
went global (2026-10-16), detections in the Middle East, Nigeria, Russia, the North
Sea, etc. needed the same treatment: gas flares, refineries, steel and cement works
and thermal power plants all read as "fire" to a thermal sensor.

Two open, attribution-licensed sources, merged into one stage file:

  1. WRI Global Power Plant Database v1.3 (CC BY 4.0)
     https://github.com/wri/global-power-plant-database
     Thermal plants only (coal / gas / oil / petcoke / biomass / waste / cogeneration),
     OUTSIDE the USA (EPA FRS already covers US facilities, and nuclear plants come
     from fetch_gibs_nuclear.py).

  2. OpenStreetMap via the Overpass API (ODbL, (c) OpenStreetMap contributors)
     man_made=flare, industrial=refinery, steel / iron / coke / cement works.
     Points inside the CONUS / Alaska boxes are skipped so validated US behaviour
     (EPA-driven) does not shift; Canada and Mexico gain coverage they never had.

Output: data/_stage_global_industrial.json  { generated_utc, sources:{...}, sites:[...] }
Each site: lat, lng, source_id, source_type, name, buffer_m, provenance.

Resilience: each source is independent. If one fails (Overpass is frequently at
capacity), its rows from the previous stage file are carried forward, so a bad
week never silently drops coverage. Exit code is 0 unless BOTH sources fail AND
there is no previous stage to fall back on.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUT = os.path.join('data', '_stage_global_industrial.json')
UA = 'firestorm-industrial-sites/1.0 (+https://github.com/Deasus/firestorm-industrial-sites)'

GPPD_URL = ('https://raw.githubusercontent.com/wri/global-power-plant-database/'
            'master/output_database/global_power_plant_database.csv')
GPPD_THERMAL = {'Coal', 'Gas', 'Oil', 'Petcoke', 'Biomass', 'Waste', 'Cogeneration'}
GPPD_BUFFER_M = {'Coal': 750, 'Gas': 750, 'Oil': 750, 'Petcoke': 750, 'Cogeneration': 750,
                 'Biomass': 500, 'Waste': 500}

OVERPASS = ['https://overpass-api.de/api/interpreter',
            'https://overpass.private.coffee/api/interpreter',
            'https://overpass.kumi.systems/api/interpreter']
OSM_QUERY = """[out:json][timeout:900][maxsize:1073741824];
(
  nwr["man_made"="flare"];
  nwr["industrial"="refinery"];
  nwr["industrial"~"^(steel_mill|steelmaking|steelworks|steel|cement|coke_oven)$"];
  nwr["man_made"="works"]["product"~"steel|iron|coke|cement",i];
);
out center tags qt;"""
# Buffer per OSM class: flares are point stacks; refineries and steelworks are
# km-scale sites whose flare stacks and furnaces sit anywhere inside the fence.
OSM_BUFFER_M = {'gas_flare': 500, 'refinery': 2000, 'steelworks': 1500, 'cement': 1000}

# Skip OSM points inside these boxes (EPA FRS is authoritative there).
US_BOXES = [(24.0, 49.5, -125.0, -66.5),     # CONUS  (minlat, maxlat, minlng, maxlng)
            (51.0, 72.0, -170.0, -129.5),     # Alaska
            (18.5, 22.5, -160.5, -154.5)]     # Hawaii


def _in_us(lat, lng):
    return any(a <= lat <= b and c <= lng <= d for a, b, c, d in US_BOXES)


def _get(url, data=None, timeout=900):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_gppd():
    body = _get(GPPD_URL, timeout=120).decode('utf-8', errors='replace')
    sites = []
    for r in csv.DictReader(io.StringIO(body)):
        fuel = r.get('primary_fuel') or ''
        if fuel not in GPPD_THERMAL or r.get('country') == 'USA':
            continue
        try:
            lat, lng = float(r['latitude']), float(r['longitude'])
        except (TypeError, ValueError, KeyError):
            continue
        sites.append({
            'lat': round(lat, 5), 'lng': round(lng, 5),
            'source_id': f"wri_gppd:{r.get('gppd_idnr')}",
            'source_type': f'power_plant_{fuel.lower()}',
            'name': f"{r.get('name') or 'Power plant'} ({fuel}, {r.get('capacity_mw') or '?'} MW, {r.get('country_long') or r.get('country')})",
            'buffer_m': GPPD_BUFFER_M.get(fuel, 750),
            'provenance': 'WRI Global Power Plant Database v1.3 (CC BY 4.0)',
        })
    return sites


def _osm_class(tags):
    if tags.get('man_made') == 'flare':
        return 'gas_flare'
    ind = (tags.get('industrial') or '').lower()
    prod = (tags.get('product') or '').lower()
    if ind == 'refinery':
        return 'refinery'
    if ind == 'cement' or 'cement' in prod:
        return 'cement'
    return 'steelworks'


def fetch_osm():
    data = urllib.parse.urlencode({'data': OSM_QUERY}).encode()
    last = None
    for attempt in range(6):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            body = _get(url, data=data, timeout=960)
            doc = json.loads(body)
            break
        except Exception as e:  # capacity 429/502/504, timeouts, HTML error pages
            last = e
            wait = min(30 * 2 ** attempt, 300)
            sys.stderr.write(f'[osm] attempt {attempt + 1} via {url} failed: {e}; retry in {wait}s\n')
            time.sleep(wait)
    else:
        raise RuntimeError(f'Overpass unavailable after retries: {last}')
    sites = []
    for el in doc.get('elements', []):
        if el.get('type') == 'node':
            lat, lng = el.get('lat'), el.get('lon')
        else:
            c = el.get('center') or {}
            lat, lng = c.get('lat'), c.get('lon')
        if lat is None or lng is None or _in_us(lat, lng):
            continue
        tags = el.get('tags') or {}
        cls = _osm_class(tags)
        name = tags.get('name:en') or tags.get('name') or {'gas_flare': 'Gas flare', 'refinery': 'Refinery',
                                                          'steelworks': 'Steel / iron works', 'cement': 'Cement works'}[cls]
        sites.append({
            'lat': round(float(lat), 5), 'lng': round(float(lng), 5),
            'source_id': f"osm:{el.get('type')}/{el.get('id')}",
            'source_type': cls,
            'name': name,
            'buffer_m': OSM_BUFFER_M[cls],
            'provenance': 'OpenStreetMap contributors (ODbL)',
        })
    return sites


def main():
    prev = {}
    try:
        with open(OUT) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    prev_sites = prev.get('sites', [])
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    out = {'generated_utc': now, 'sources': {}, 'sites': []}
    ok = 0
    for key, fn, prefix in (('wri_gppd', fetch_gppd, 'wri_gppd:'), ('osm', fetch_osm, 'osm:')):
        try:
            sites = fn()
            out['sites'].extend(sites)
            out['sources'][key] = {'status': 'fresh', 'count': len(sites), 'generated_utc': now}
            ok += 1
            sys.stderr.write(f'[global-industrial] {key}: {len(sites)} sites\n')
        except Exception as e:
            kept = [s for s in prev_sites if str(s.get('source_id', '')).startswith(prefix)]
            out['sites'].extend(kept)
            out['sources'][key] = {'status': 'carried_forward', 'count': len(kept), 'error': str(e)[:200],
                                   'generated_utc': (prev.get('sources', {}).get(key) or {}).get('generated_utc')}
            sys.stderr.write(f'[global-industrial] {key} FAILED ({e}); carried forward {len(kept)} sites\n')
    if not ok and not prev_sites:
        sys.stderr.write('[global-industrial] no source succeeded and no previous stage; nothing written\n')
        return 1
    os.makedirs('data', exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    os.replace(tmp, OUT)
    by = {}
    for s in out['sites']:
        by[s['source_type']] = by.get(s['source_type'], 0) + 1
    sys.stderr.write(f'[global-industrial] wrote {len(out["sites"])} sites: {by}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
