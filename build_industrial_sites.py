#!/usr/bin/env python3
"""Merge the three staged source JSONs into a single industrial_sites.geojson
that firestorm-firms-data consumes at ingest time.

Inputs (produced by the three fetch_*.py scripts):
  data/_stage_epa_frs.json     — EPA FRS thermal-emitter facilities
  data/_stage_gvp.json         — Smithsonian GVP Holocene volcanoes

Output:
  data/industrial_sites.geojson       — merged FeatureCollection of Points
  data/industrial_sites_meta.json     — per-source manifest
  data/industrial_sites.min.geojson   — same but rounded to 4dp, no whitespace
                                        (~1/3 the size — for browser fetch)

Note: land_10m.geojson is a separate file (polygons, distance-to-land test
lives in the FIRMS pipeline). This script does NOT touch it.
"""
import json
import os
import sys
from datetime import datetime, timezone

STAGE_FRS = os.path.join('data', '_stage_epa_frs.json')
STAGE_GVP = os.path.join('data', '_stage_gvp.json')
LAND_POLY = os.path.join('data', 'land_10m.geojson')   # existence check only

OUT_GEOJSON = os.path.join('data', 'industrial_sites.geojson')
OUT_MIN     = os.path.join('data', 'industrial_sites.min.geojson')
OUT_META    = os.path.join('data', 'industrial_sites_meta.json')

# Per-class default proximity buffer for VIIRS/MODIS deconfliction.
# Empirical basis: VIIRS 375m native pixel + ~125m FIRMS geolocation error
# = 500m defensive floor for industrial point sources. Volcanoes get 5km
# to cover flank vents. Solar farms (v2) will be 375m because USPVDB
# publishes tight polygon footprints — no summit offset to worry about.
DEFAULT_BUFFER_M = {
    'industrial': 500,
    'volcano':    5000,
}


def _load(path: str) -> dict | None:
    if not os.path.exists(path):
        sys.stderr.write(f'[merge] MISSING {path}\n')
        return None
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            sys.stderr.write(f'[merge] {path} corrupt: {e}\n')
            return None


def _feat(lat: float, lng: float, props: dict) -> dict:
    return {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [round(lng, 5), round(lat, 5)]},
        'properties': props,
    }


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    frs = _load(STAGE_FRS)
    gvp = _load(STAGE_GVP)
    if not frs and not gvp:
        sys.exit('[merge] FATAL: both source stages missing; nothing to merge')

    features: list[dict] = []
    source_summary: list[dict] = []

    # ---- EPA FRS ----
    if frs:
        n_before = len(features)
        for r in frs.get('facilities', []):
            features.append(_feat(r['lat'], r['lng'], {
                'source_id':  f'epa_frs:{r["reg_id"]}',
                'source_type': r['src_type'],
                'class':      r['class'],
                'naics':      r['naics'],
                'name':       r['name'],
                'state':      r['state'],
                'buffer_m':   DEFAULT_BUFFER_M['industrial'],
                'provenance': 'EPA FRS national_single.zip',
            }))
        source_summary.append({
            'source':      'epa_frs',
            'generated':   frs.get('generated_utc'),
            'count':       len(features) - n_before,
            'naics_used':  frs.get('naics_filter', []),
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} EPA FRS facilities\n')

    # ---- GVP volcanoes ----
    if gvp:
        n_before = len(features)
        for v in gvp.get('volcanoes', []):
            features.append(_feat(v['lat'], v['lng'], {
                'source_id':   v['source_id'],
                'source_type': 'volcano',
                'class':       'volcano',
                'name':        v['name'],
                'country':     v.get('country'),
                'volcano_type': v.get('volcano_type'),
                'last_eruption_year': v.get('last_eruption_year'),
                'buffer_m':    v.get('buffer_m', DEFAULT_BUFFER_M['volcano']),
                'provenance':  'Smithsonian GVP-VOTW Holocene Volcanoes',
            }))
        source_summary.append({
            'source':    'gvp_holocene',
            'generated': gvp.get('generated_utc'),
            'count':     len(features) - n_before,
        })
        sys.stderr.write(f'[merge] +{len(features) - n_before} GVP volcanoes\n')

    # ---- Land polygon file existence check (not merged, just reported) ----
    land_ok = os.path.exists(LAND_POLY) and os.path.getsize(LAND_POLY) > 100_000
    source_summary.append({
        'source':    'natural_earth_land_10m',
        'file':      LAND_POLY if land_ok else None,
        'available': land_ok,
    })

    fc = {
        'type':     'FeatureCollection',
        'generated_utc': now_iso,
        'version':  '1.0.0',
        'features': features,
    }

    # Write the pretty-formatted GeoJSON (for git-diffing)
    os.makedirs(os.path.dirname(OUT_GEOJSON), exist_ok=True)
    for path, kwargs in [
        (OUT_GEOJSON, {'indent': None, 'separators': (',', ':')}),
        (OUT_MIN,     {'separators': (',', ':')}),
    ]:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(fc, f, **kwargs)
        os.replace(tmp, path)

    # Manifest — every claim in the output must be back-linkable to a source
    meta = {
        'generated_utc':   now_iso,
        'total_features':  len(features),
        'sources':         source_summary,
        'consumer':        'firestorm-firms-data/fetch_firms.py',
        'buffer_defaults': DEFAULT_BUFFER_M,
    }
    tmp = OUT_META + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, OUT_META)

    size_kb = os.path.getsize(OUT_MIN) / 1024
    sys.stderr.write(f'[merge] wrote {OUT_GEOJSON} '
                     f'({len(features)} features, {size_kb:.0f} KB minified)\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
