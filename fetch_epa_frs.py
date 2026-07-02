#!/usr/bin/env python3
"""EPA Facility Registry Service (FRS) fetch for thermal-emitter deconfliction.

Pulls the national FRS bulk zip once (~340 MB), extracts the single flat
CSV (NATIONAL_SINGLE.CSV), filters to facilities whose NAICS_CODES column
contains any of our thermal-emitter codes, emits a slim JSON with lat/lng +
name + state + NAICS + class label. Output is consumed by
build_industrial_sites.py into the merged industrial_sites.geojson.

Verified schema 2026-07-02 against state_single_ri.zip (same shape as
national_single.zip): 39 columns, NAICS_CODES at column 27 as a
comma-or-pipe-delimited string of 6-digit NAICS codes. Coordinates are
LATITUDE83 / LONGITUDE83 (NAD83 decimal degrees).

Source: https://ordsext.epa.gov/FLA/www3/state_files/national_single.zip
Docs (Envirofacts landing): https://www.epa.gov/frs
"""
import csv
import io
import json
import os
import re
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
NAICS_CLASS = {
    '324110': ('petroleum_refinery',      'industrial'),
    '221112': ('fossil_fuel_power',        'industrial'),
    '331110': ('steel_mill',               'industrial'),
    '327310': ('cement_plant',             'industrial'),
    '562213': ('waste_combustion',         'industrial'),
}

BULK_URL = 'https://ordsext.epa.gov/FLA/www3/state_files/national_single.zip'
HTTP_TIMEOUT = 300
OUT_JSON = os.path.join('data', '_stage_epa_frs.json')
CACHE_ZIP = os.path.join('data', '_cache_national_single.zip')

# NAICS strings inside NAICS_CODES may be separated by any of: pipe, comma,
# semicolon (observed across multiple state files). Split on any of them.
_NAICS_SPLIT = re.compile(r'[|,;\s]+')


def download_bulk() -> str:
    """Download the national FRS bulk zip. Cache 7d — bulk refreshes ~monthly."""
    if os.path.exists(CACHE_ZIP):
        age_s = (datetime.now(tz=timezone.utc).timestamp()
                 - os.path.getmtime(CACHE_ZIP))
        if age_s < 7 * 86400:
            sys.stderr.write(f'[frs] cache hit ({age_s/86400:.1f}d old): {CACHE_ZIP}\n')
            return CACHE_ZIP

    os.makedirs(os.path.dirname(CACHE_ZIP), exist_ok=True)
    tmp = CACHE_ZIP + '.tmp'
    sys.stderr.write(f'[frs] downloading {BULK_URL} → {tmp}\n')
    req = urllib.request.Request(BULK_URL, headers={
        'User-Agent': 'firestorm-industrial-sites/1.0',
        'Accept': 'application/zip,*/*',
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        total = int(resp.headers.get('Content-Length', 0))
        bytes_read = 0
        with open(tmp, 'wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)
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
    """Stream-parse NATIONAL_SINGLE.CSV. For each row whose NAICS_CODES
    string contains any of our thermal NAICS, emit one record per matching
    NAICS (a facility with two matching NAICS emits two records — each is
    a different classification we might want to surface separately).
    """
    out: list[dict] = []
    n_rows = 0
    n_geocoded = 0
    n_by_naics: dict[str, int] = {}

    with zipfile.ZipFile(zip_path) as zf:
        csv_name = next((n for n in zf.namelist() if n.upper().endswith('.CSV')), None)
        if not csv_name:
            sys.exit(f'[frs] FATAL: no CSV in zip. Contents: {zf.namelist()}')
        sys.stderr.write(f'[frs] reading {csv_name}...\n')

        with zf.open(csv_name) as fh:
            text = io.TextIOWrapper(fh, encoding='utf-8', errors='replace')
            reader = csv.DictReader(text)
            for row in reader:
                n_rows += 1
                naics_str = (row.get('NAICS_CODES') or '').strip()
                if not naics_str:
                    continue
                # Find matching NAICS codes for this facility
                matching = set()
                for tok in _NAICS_SPLIT.split(naics_str):
                    tok = tok.strip()
                    if tok in NAICS_CLASS:
                        matching.add(tok)
                if not matching:
                    continue
                # Parse coordinates (NAD83 decimal degrees)
                try:
                    lat = float(row.get('LATITUDE83') or '')
                    lng = float(row.get('LONGITUDE83') or '')
                except (TypeError, ValueError):
                    continue
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                    continue
                # Reject 0,0 unknown-location sentinel
                if abs(lat) < 0.01 and abs(lng) < 0.01:
                    continue
                n_geocoded += 1
                name = (row.get('PRIMARY_NAME') or 'UNKNOWN').strip().upper()[:80]
                state = (row.get('STATE_CODE') or '').strip()[:2]
                reg_id = (row.get('REGISTRY_ID') or '').strip()
                for naics in matching:
                    src_type, cls = NAICS_CLASS[naics]
                    n_by_naics[naics] = n_by_naics.get(naics, 0) + 1
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
                if n_rows % 200_000 == 0:
                    sys.stderr.write(f'[frs]   scanned {n_rows:,} rows, {n_geocoded:,} matches so far\n')

    sys.stderr.write(f'[frs] scanned {n_rows:,} total rows; {n_geocoded:,} geocoded facility matches; {len(out)} emit records\n')
    for k in sorted(n_by_naics):
        src_type = NAICS_CLASS[k][0]
        sys.stderr.write(f'[frs]   {k} ({src_type}): {n_by_naics[k]}\n')
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

    if len(rows) < 100:
        sys.stderr.write('[frs] WARN: fewer than 100 facilities — schema may have drifted\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
