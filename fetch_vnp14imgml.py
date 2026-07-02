#!/usr/bin/env python3
"""VNP14IMGML monthly archive fetch — NASA's own persistent static-thermal-
source classification for VIIRS 375m fire detections.

VNP14IMGML is the University of Maryland monthly archive of VIIRS 375m fire
detections (Suomi-NPP + NOAA-20 + NOAA-21). Every detection carries a
`Type` field that NASA's OWN detection algorithm assigns:

    Type = 0    Presumed vegetation fire
    Type = 1    Active volcano
    Type = 2    Other static land source (industrial: refinery, gas flare, steel mill, kiln, etc.)
    Type = 3    Offshore detection (sun glint, ship, offshore flare platform)

The FIRMS Advanced Mode UI's "Static Thermal Anomalies" layer group in the
right sidebar (Mask / Detections / Industrial Plants / Power Plants) is
almost certainly rendered from this Type field — NASA doesn't publish the
classification anywhere else. The NRT (near-real-time) FIRMS feeds STRIP
this field out; only the monthly archive keeps it.

By ingesting 12 months of monthly archives, filtering to Type ∈ {1, 2, 3},
and clustering at 375m radius (VIIRS native pixel), we reconstruct NASA's
own static-source classification for use as an authoritative persistent-
source overlay. This is the "precision" complement to the "coverage" of
EPA FRS + GVP + Natural Earth land polygon.

Source: SFTP fire@fuoco.geog.umd.edu:/VIIRS/monthly/VNP14IMGML/
Ref:    MODIS Collection 6 Active Fire Product User's Guide §4.3.5 + §3.7

Path resolution:
  Primary: SFTP fuoco.geog.umd.edu (anonymous, password "burnt")
  Fallback: none — this is the SOLE canonical source. If SFTP fails, we
  skip this refresh; the previous industrial_sites.geojson (built from
  EPA FRS + GVP + GIBS nuclear + last-good VNP14IMGML) stays in place.

Cadence: monthly (files publish ~1 month after end-of-month; e.g. June's
data lands early August). Weekly cron catches new files defensively.

Output: data/_stage_firms_persistent.json — one point per persistent-
source cluster with class ∈ {volcano_firms, industrial_firms, offshore_firms}.
"""
import gzip
import io
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

# The 12 monthly files we want. NASA reprocesses periodically so we take
# the last-12-months window rather than "everything since 2012" — long-tail
# accuracy vs 12-year permanent-source drift isn't worth the archive size.
LOOKBACK_MONTHS = 12

# 375m native VIIRS pixel — the clustering radius. Detections within this
# distance across multiple months collapse to a single persistent-source
# point.
CLUSTER_RADIUS_M = 375
EARTH_RADIUS_M = 6_371_000.0

# Minimum months a location must fire before we call it "persistent". Peer-
# reviewed VNF literature uses 5-10; 6 is a defensive middle-of-consensus
# (12 months of data → require 50% duty cycle → 6 months → persistent).
# Tunable via env for research-mode experimentation.
MIN_PERSISTENCE_MONTHS = int(os.environ.get('MIN_PERSISTENCE_MONTHS', '6'))

# SFTP credentials from the MODIS C6 User Guide §4.3.2 (documented anon path).
SFTP_HOST = 'fuoco.geog.umd.edu'
SFTP_USER = 'fire'
SFTP_PASS = 'burnt'
SFTP_ROOT = '/VIIRS/monthly/VNP14IMGML'

# Same directory served over HTTPS — added by UMD post-2023 as an SFTP
# alternative. The URL structure isn't documented in the User Guide but
# it's the pattern used across other UMD fire-product mirrors. We try
# this only if SFTP fails (some corporate networks block SFTP).
HTTPS_MIRROR_BASE = 'https://modis-fire.umd.edu/pub/VIIRS/monthly/VNP14IMGML'

OUT_JSON = os.path.join('data', '_stage_firms_persistent.json')
CACHE_DIR = os.path.join('data', '_cache_vnp14imgml')

# Column indices in VNP14IMGML ASCII CSV format (per User Guide §4.3.5)
# Header row present in modern files: YYYYMMDD,HHMM,sat,lat,lon,T4,T5,sample,pixarea,frp,conf,type
COL_LAT   = 'lat'
COL_LON   = 'lon'
COL_TYPE  = 'type'
COL_CONF  = 'conf'
COL_FRP   = 'frp'
COL_DATE  = 'YYYYMMDD'


def target_months() -> list[str]:
    """Return the last N months as 'YYYYMM' strings, newest first.
    Files publish ~1 month after end-of-month, so we skip the current
    month (would return 404) and start at last month."""
    today = datetime.now(timezone.utc).replace(day=1)
    out = []
    # Skip current + previous month — the latest publish lags by ~1 mo.
    cursor = today - timedelta(days=32)   # → last month
    cursor = cursor.replace(day=1) - timedelta(days=1)  # → month before that
    cursor = cursor.replace(day=1)
    for _ in range(LOOKBACK_MONTHS):
        out.append(cursor.strftime('%Y%m'))
        # Step back one month
        prev = cursor - timedelta(days=1)
        cursor = prev.replace(day=1)
    return out


def sftp_download(remote_path: str, local_path: str) -> bool:
    """Fetch a single file via SFTP. Uses OpenSSH sftp binary (present on
    every GHA runner) with sshpass for password auth. Returns True on
    success."""
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    # Note: sshpass may not be installed by default; if unavailable we fall
    # through to HTTPS. GHA workflow installs sshpass in its `apt install` step.
    try:
        # Non-interactive: `-o BatchMode=no` allows password; `-o StrictHostKeyChecking=accept-new`
        # avoids first-connect prompt in CI. sshpass supplies the password.
        cmd = [
            'sshpass', '-p', SFTP_PASS,
            'sftp',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-b', '-',   # read commands from stdin
            f'{SFTP_USER}@{SFTP_HOST}',
        ]
        sftp_cmd = f'get {remote_path} {local_path}\nbye\n'
        r = subprocess.run(cmd, input=sftp_cmd, capture_output=True,
                           text=True, timeout=180)
        if r.returncode == 0 and os.path.exists(local_path) and os.path.getsize(local_path) > 1024:
            return True
        sys.stderr.write(f'[vnp] sftp failed rc={r.returncode}: {r.stderr[:200]}\n')
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f'[vnp] sftp path unavailable: {e}\n')
        return False


def https_download(url: str, local_path: str) -> bool:
    """Fallback: HTTPS mirror. Some corporate networks block SFTP."""
    import urllib.request
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        req = urllib.request.Request(url, headers={
            'User-Agent': 'firestorm-industrial-sites/1.0'})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
        if len(data) < 1024:
            return False
        with open(local_path, 'wb') as f:
            f.write(data)
        return True
    except Exception as e:
        sys.stderr.write(f'[vnp] https {url} failed: {e}\n')
        return False


def download_month(yyyymm: str) -> Optional[str]:
    """Return local path to the downloaded (possibly gzipped) monthly file,
    or None if all sources failed."""
    # File naming per User Guide: VNP14IMGML.YYYYMM.CC.VV.txt.gz
    # We don't know CC/VV in advance; probe the SFTP directory listing.
    # Modern GHA/local practice: use paramiko for programmatic listing.
    # For simplicity, we try the well-known naming variants:
    candidates = [
        f'VNP14IMGML.{yyyymm}.061.03.txt.gz',
        f'VNP14IMGML.{yyyymm}.061.02.txt.gz',
        f'VNP14IMGML.{yyyymm}.061.01.txt.gz',
        f'VNP14IMGML.{yyyymm}.061.00.txt.gz',
    ]
    for fname in candidates:
        local = os.path.join(CACHE_DIR, fname)
        if os.path.exists(local) and os.path.getsize(local) > 1024:
            sys.stderr.write(f'[vnp] cache hit: {fname}\n')
            return local
        # 1) SFTP primary
        remote = f'{SFTP_ROOT}/{fname}'
        if sftp_download(remote, local):
            sys.stderr.write(f'[vnp] SFTP got {fname}\n')
            return local
        # 2) HTTPS fallback
        url = f'{HTTPS_MIRROR_BASE}/{fname}'
        if https_download(url, local):
            sys.stderr.write(f'[vnp] HTTPS got {fname}\n')
            return local
    sys.stderr.write(f'[vnp] no source found for {yyyymm} (all naming variants + both transports failed)\n')
    return None


def parse_monthly(path: str) -> list[dict]:
    """Stream the gzipped ASCII monthly file. Emit one dict per Type-in-{1,2,3}
    detection: {lat, lng, type, conf, frp, yyyymmdd}."""
    out = []
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8', errors='replace') as fh:
        # First non-empty line is the header. File format:
        #   YYYYMMDD HHMM sat lat lon T4 T5 sample pixarea frp conf type
        # Whitespace-separated (mixed spaces/tabs across versions), so we split loosely.
        header = None
        header_map = {}
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if header is None:
                header = re.split(r'\s+', line)
                header_map = {name.lower(): i for i, name in enumerate(header)}
                continue
            parts = re.split(r'\s+', line)
            try:
                lat = float(parts[header_map[COL_LAT]])
                lng = float(parts[header_map[COL_LON]])
                type_v = int(parts[header_map[COL_TYPE]])
            except (KeyError, IndexError, ValueError):
                continue
            # Keep only volcano / static / offshore (drop Type 0 = vegetation fire)
            if type_v not in (1, 2, 3):
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                continue
            try:
                conf = parts[header_map[COL_CONF]]
                frp = float(parts[header_map[COL_FRP]])
                date = parts[header_map[COL_DATE]]
            except (KeyError, IndexError, ValueError):
                conf, frp, date = '', 0.0, ''
            out.append({
                'lat': lat, 'lng': lng, 'type': type_v,
                'conf': conf, 'frp': frp, 'date': date,
            })
    return out


def cluster_persistent(all_detections: list[dict]) -> list[dict]:
    """Cluster detections at CLUSTER_RADIUS_M. A cluster becomes a
    "persistent source" when its detections span ≥ MIN_PERSISTENCE_MONTHS
    distinct months.

    Uses a simple grid-based clusterer (not sklearn's DBSCAN) so the script
    has no scipy/sklearn hard dependency — deconflict.py already needs
    sklearn for the runtime BallTree; this build script runs weekly in a
    lean environment.
    """
    # Grid cell size ≈ 0.005° ≈ 550m at equator. Any two points inside the
    # same or neighboring cells are candidates; we then filter by exact
    # haversine < CLUSTER_RADIUS_M.
    GRID = 0.005
    grid: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(all_detections):
        key = (int(d['lat'] / GRID), int(d['lng'] / GRID))
        grid.setdefault(key, []).append(i)

    seen = set()
    clusters = []
    for i, d in enumerate(all_detections):
        if i in seen:
            continue
        # Gather points in this cell + 8 neighbors
        cell = (int(d['lat'] / GRID), int(d['lng'] / GRID))
        candidates = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                candidates.extend(grid.get((cell[0]+dy, cell[1]+dx), []))
        # Filter to actual haversine < radius
        members = []
        for j in candidates:
            if j in seen:
                continue
            if _hav_m(d['lat'], d['lng'], all_detections[j]['lat'], all_detections[j]['lng']) <= CLUSTER_RADIUS_M:
                members.append(j)
        for j in members:
            seen.add(j)
        if len(members) < 2:
            continue
        # Cluster summary — centroid + type-vote + month-diversity
        members_data = [all_detections[j] for j in members]
        months = {m['date'][:6] for m in members_data if m['date']}
        if len(months) < MIN_PERSISTENCE_MONTHS:
            continue
        centroid_lat = sum(m['lat'] for m in members_data) / len(members_data)
        centroid_lng = sum(m['lng'] for m in members_data) / len(members_data)
        # Type vote — plurality wins. Ties on volcano over industrial over offshore
        # (safety-first: a volcano-plus-industrial ambiguity is more likely a real
        # volcano near a facility than not; err toward volcano classification).
        type_votes = {1: 0, 2: 0, 3: 0}
        for m in members_data:
            type_votes[m['type']] += 1
        type_winner = max(type_votes, key=lambda k: (type_votes[k], -k))
        clusters.append({
            'lat': round(centroid_lat, 5),
            'lng': round(centroid_lng, 5),
            'type': type_winner,
            'n_detections': len(members_data),
            'n_months': len(months),
            'months_present': sorted(months),
        })
    return clusters


def _hav_m(lat1, lng1, lat2, lng2) -> float:
    from math import radians, sin, cos, asin, sqrt
    lat1r, lat2r = radians(lat1), radians(lat2)
    dlat = lat2r - lat1r
    dlng = radians(lng2 - lng1)
    a = sin(dlat/2)**2 + cos(lat1r) * cos(lat2r) * sin(dlng/2)**2
    return 2 * EARTH_RADIUS_M * asin(min(1.0, sqrt(a)))


TYPE_META = {
    1: ('volcano_firms',    'volcano',  'NASA VNP14IMGML persistent volcano detection'),
    2: ('industrial_firms', 'industrial', 'NASA VNP14IMGML persistent static-industrial detection'),
    3: ('offshore_firms',   'offshore', 'NASA VNP14IMGML persistent offshore detection'),
}


def main() -> int:
    months = target_months()
    sys.stderr.write(f'[vnp] target months (newest first): {months}\n')

    all_dets: list[dict] = []
    months_fetched = 0
    for yyyymm in months:
        path = download_month(yyyymm)
        if not path:
            continue
        try:
            dets = parse_monthly(path)
            sys.stderr.write(f'[vnp] {yyyymm}: parsed {len(dets)} type∈{{1,2,3}} rows\n')
            all_dets.extend(dets)
            months_fetched += 1
        except Exception as e:
            sys.stderr.write(f'[vnp] parse failed on {path}: {e}\n')

    if not all_dets:
        sys.stderr.write('[vnp] no data fetched — skipping VNP14IMGML stage\n')
        # Emit an empty-but-well-formed stage so build_industrial_sites.py doesn't fail
        _write_out(months, months_fetched, [])
        return 0

    sys.stderr.write(f'[vnp] {len(all_dets)} total detections across {months_fetched} months\n')
    clusters = cluster_persistent(all_dets)
    sys.stderr.write(f'[vnp] {len(clusters)} persistent-source clusters (≥{MIN_PERSISTENCE_MONTHS} months)\n')

    by_type = {}
    for c in clusters:
        by_type[c['type']] = by_type.get(c['type'], 0) + 1
    for t, name in ((1,'volcano'), (2,'industrial'), (3,'offshore')):
        sys.stderr.write(f'[vnp]   type={t} ({name}): {by_type.get(t, 0)}\n')

    _write_out(months, months_fetched, clusters)
    return 0


def _write_out(months, months_fetched, clusters):
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    payload = {
        'generated_utc': now_iso,
        'source':        'NASA VNP14IMGML (UMD monthly archive)',
        'window_months': len(months),
        'months_fetched': months_fetched,
        'min_persistence_months': MIN_PERSISTENCE_MONTHS,
        'cluster_radius_m': CLUSTER_RADIUS_M,
        'count':         len(clusters),
        'clusters':      clusters,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[vnp] wrote {OUT_JSON}: {len(clusters)} clusters\n')


if __name__ == '__main__':
    raise SystemExit(main())
