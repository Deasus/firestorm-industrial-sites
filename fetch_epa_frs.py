#!/usr/bin/env python3
"""EPA Facility Registry Service (FRS) fetch for thermal-emitter deconfliction.

Pulls the national FRS bulk zip once (~340 MB), extracts facility site rows,
filters to thermal-emitter NAICS codes, joins facility → NAICS via the
NAICS_CODES table, emits a slim JSON with lat/lng + name + state + NAICS +
class label. Output is consumed by build_industrial_sites.py into the
merged industrial_sites.geojson.

Why we don't hit the Envirofacts REST API row-by-row:
- The `enviro.epa.gov/enviro/efservice` host 301s to `data.epa.gov/efservice`
  which returns paginated XML/JSON but caps at ~500-1000 rows per call and
  is flaky (~5% 500s on repeat probing 2026-07-02). For a global US-wide
  filter across 5 NAICS codes we'd need 20+ paginated calls; the bulk zip
  is one call, one moving part.
- Bulk `national_single.zip` last-modified 2026-06-04 — monthly refresh
  cadence, matches our weekly GHA cron slack. Sufficient for a slow-changing
  facility registry.

Source of truth: https://ordsext.epa.gov/FLA/www3/state_files/
Docs (Envirofacts landing): https://www.epa.gov/frs
"""
import csv
import io
import json
import os
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone

# Thermal-emitter NAICS codes we care about for VIIRS/MODIS deconfliction.
# All are documented as significant IR emitters in the fire-detection lit:
#   324110 = petroleum refineries              (~135 US facilities, permanent 350K+ IR)
#   221112 = fossil-fuel electric power gen    (~1,500 US, stack + steam plume)
#   331110 = iron & steel mills                (~80 US, arc/blast furnaces)
#   327310 = cement manufacturing              (~90 US, 1400-1500C kilns)
#   562213 = solid waste combustion + energy   (~75 US, incinerators)
# Class label per NAICS so downstream consumers can style / prioritize.
NAICS_CLASS = {
    '324110': ('petroleum_refinery',      'industrial'),
    '221112': ('fossil_fuel_power',        'industrial'),
    '331110': ('steel_mill',               'industrial'),
    '327310': ('cement_plant',             'industrial'),
    '562213': ('waste_combustion',         'industrial'),
}

BULK_URL = 'https://ordsext.epa.gov/FLA/www3/state_files/national_single.zip'
HTTP_TIMEOUT = 300      # 340 MB over slow connections; be generous
OUT_JSON = os.path.join('data', '_stage_epa_frs.json')
CACHE_ZIP = os.path.join('data', '_cache_national_single.zip')


def download_bulk() -> str:
    """Download the national FRS bulk zip to a stable cache path.
    Returns the local cache path. Idempotent: skips download if the cached
    zip is <7d old (bulk refreshes ~monthly).
    """
    if os.path.exists(CACHE_ZIP):
        age_s = (datetime.now(tz=timezone.utc).timestamp()
                 - os.path.getmtime(CACHE_ZIP))
        if age_s < 7 * 86400:
            sys.stderr.write(f'[frs] cache hit ({age_s/86400:.1f}d old): {CACHE_ZIP}\n')
            return CACHE_ZIP

    os.makedirs(os.path.dirname(CACHE_ZIP), exist_ok=True)
    tmp = CACHE_ZIP + '.tmp'
    sys.stderr.write(f'[frs] downloading {BULK_URL} → {tmp}\n')
    req = urllib.request.Request(BULK_URL,
        headers={'User-Agent': 'firestorm-industrial-sites/1.0',
                 'Accept': 'application/zip,*/*'})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        # Stream to disk — 340 MB in memory is fine on GHA (7 GB RAM) but
        # streaming is cheaper and prints progress.
        total = int(resp.headers.get('Content-Length', 0))
        bytes_read = 0
        with open(tmp, 'wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)   # 1 MB
                if not chunk:
                    break
                f.write(chunk)
                bytes_read += len(chunk)
                if total and bytes_read % (32 * 1024 * 1024) < len(chunk):
                    pct = 100.0 * bytes_read / total
                    sys.stderr.write(f'[frs]   {bytes_read/1e6:.0f}/{total/1e6:.0f} MB ({pct:.0f}%)\n')
    os.replace(tmp, CACHE_ZIP)
    sys.stderr.write(f'[frs] downloaded {bytes_read/1e6:.0f} MB\n')
    return CACHE_ZIP


def extract_matching_facilities(zip_path: str) -> list[dict]:
    """Open the FRS bulk zip and stream-parse the two CSVs we need:
      - NATIONAL_NAICS_FILE.CSV  → facility_id → set(naics_code)
      - NATIONAL_SINGLE.CSV      → facility_id → lat/lng/name/state

    Return list of enriched facility dicts, one per matching NAICS.
    A facility with two matching NAICS codes emits two records (they may
    be spatially distinct sub-facilities, and each class matters for the
    operator label).
    """
    facilities_matching_naics: dict[str, list[str]] = {}   # reg_id → [naics_code, ...]
    with zipfile.ZipFile(zip_path) as zf:
        # Locate the NAICS join table. The bulk zip's exact filename has
        # historically been NATIONAL_NAICS_FILE.CSV — but we look it up
        # case-insensitively in case EPA rebrands.
        naics_names = [n for n in zf.namelist() if n.lower().endswith('naics_file.csv') or 'naics' in n.lower() and n.lower().endswith('.csv')]
        if not naics_names:
            sys.exit(f'[frs] FATAL: no NAICS CSV found in bulk zip. Contents: {zf.namelist()[:20]}')
        naics_file = naics_names[0]
        sys.stderr.write(f'[frs] reading NAICS join: {naics_file}\n')

        with zf.open(naics_file) as fh:
            text = io.TextIOWrapper(fh, encoding='utf-8', errors='replace')
            reader = csv.DictReader(text)
            n_rows = 0
            for row in reader:
                n_rows += 1
                # Common header variants across FRS versions
                reg_id = (row.get('REGISTRY_ID')
                          or row.get('REGISTRY_KEY')
                          or row.get('FRS_FACILITY_SITE_ID')
                          or '').strip()
                naics = (row.get('NAICS_CODE') or row.get('naics_code') or '').strip()
                if not reg_id or not naics:
                    continue
                if naics in NAICS_CLASS:
                    facilities_matching_naics.setdefault(reg_id, []).append(naics)
            sys.stderr.write(f'[frs] scanned {n_rows} NAICS rows; matched {len(facilities_matching_naics)} facilities\n')

        # Now the facility site table (lat/lng/name/state)
        site_names = [n for n in zf.namelist() if 'facility' in n.lower() and n.lower().endswith('.csv')]
        # Fall back to NATIONAL_SINGLE.CSV explicitly if the pattern above fails
        for candidate in ['NATIONAL_SINGLE.CSV', 'NATIONAL_FACILITY_FILE.CSV', 'FRS_FACILITY_SITE.CSV']:
            if candidate in zf.namelist():
                site_names.insert(0, candidate)
                break
        if not site_names:
            sys.exit(f'[frs] FATAL: no facility CSV found. Contents: {zf.namelist()[:20]}')
        site_file = site_names[0]
        sys.stderr.write(f'[frs] reading facility sites: {site_file}\n')

        out: list[dict] = []
        with zf.open(site_file) as fh:
            text = io.TextIOWrapper(fh, encoding='utf-8', errors='replace')
            reader = csv.DictReader(text)
            n_rows = 0
            n_matched = 0
            n_geocoded = 0
            for row in reader:
                n_rows += 1
                reg_id = (row.get('REGISTRY_ID')
                          or row.get('REGISTRY_KEY')
                          or row.get('FRS_FACILITY_SITE_ID')
                          or '').strip()
                if reg_id not in facilities_matching_naics:
                    continue
                n_matched += 1
                # Coordinates are decimal degrees (WGS84 per FRS docs)
                try:
                    lat = float(row.get('LATITUDE83') or row.get('LATITUDE') or '')
                    lng = float(row.get('LONGITUDE83') or row.get('LONGITUDE') or '')
                except (TypeError, ValueError):
                    continue
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                    continue
                # Reject 0,0 (FRS uses this as "unknown location" sentinel for
                # some legacy records — skip them, they'd cluster over the
                # Atlantic and produce absurd nearest-neighbor results)
                if abs(lat) < 0.01 and abs(lng) < 0.01:
                    continue
                n_geocoded += 1
                name = (row.get('PRIMARY_NAME') or row.get('FACILITY_NAME')
                        or row.get('STD_NAME') or 'UNKNOWN').strip().upper()[:80]
                state = (row.get('STATE_CODE') or row.get('STATE_NAME') or '').strip()[:2]
                for naics in facilities_matching_naics[reg_id]:
                    src_type, cls = NAICS_CLASS[naics]
                    out.append({
                        'reg_id':   reg_id,
                        'naics':    naics,
                        'src_type': src_type,
                        'class':    cls,
                        'name':     name,
                        'state':    state,
                        'lat':      round(lat, 5),
                        'lng':      round(lng, 5),
                    })
            sys.stderr.write(f'[frs] scanned {n_rows} facility rows; '
                             f'{n_matched} matched NAICS; {n_geocoded} geocoded\n')
    return out


def main() -> int:
    zip_path = download_bulk()
    rows = extract_matching_facilities(zip_path)

    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = {
        'generated_utc': now_iso,
        'source':        'EPA FRS bulk (national_single.zip)',
        'source_url':    BULK_URL,
        'naics_filter':  list(NAICS_CLASS.keys()),
        'count':         len(rows),
        'facilities':    rows,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[frs] wrote {OUT_JSON}: {len(rows)} facilities\n')

    # By-class count for logging (operator-visible in the GHA run summary)
    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r['src_type']] = by_class.get(r['src_type'], 0) + 1
    for k in sorted(by_class):
        sys.stderr.write(f'[frs]   {k}: {by_class[k]}\n')

    if len(rows) < 100:
        sys.stderr.write('[frs] WARN: fewer than 100 facilities — schema may have drifted\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
