#!/usr/bin/env python3
"""Self-mine persistent thermal sources from FIRESTORM's own FIRMS archive.

REPLACES the earlier fetch_vnp14imgml.py which was chasing a NASA product
that doesn't exist in the official catalog (verified 2026-07-02 via CMR
zero-results search + UMD-hosted files are UMD-local, not NASA-verified).

NASA's own "Type" field in their monthly archive is computed by UMD from
a 90-day trailing window clustering algorithm. We can replicate the same
algorithm in real-time on our own FIRMS archive — same input data
(NASA FIRMS operational feed), same classification logic. Result:
"proximate_persistent_other" flag class that catches persistent thermal
sources EPA FRS + GVP don't cover (illegal burn pits, off-grid flares,
unregistered agricultural burn areas, etc.).

Method:
1. Walk `firestorm-firms-data` git history back 90 days.
2. Sample every N-th commit (~1 commit/day is enough — that repo commits
   ~4/hour, so we sample every ~100th commit) to keep total fetches < 90.
3. Fetch each commit's viirs.json via raw.githubusercontent.com.
4. Union all detections; grid-quantize at 375m (VIIRS native pixel).
5. Any grid cell with ≥N distinct-day detections = persistent source.
6. Emit as `class: 'persistent_other'` features into the merged
   industrial_sites.geojson via build_industrial_sites.py.

Cadence: weekly (via the existing update-industrial-sites.yml GHA cron).
90 days of samples via ~90 curl fetches = ~5 min total wall time on GHA.

Runs against the SAME data we're deconflicting, so this closes a
feedback loop: FIRMS detections we saw today feed the persistent-source
map that filters FIRMS detections tomorrow.
"""
import io
import json
import math
import os
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional


FIRMS_REPO         = 'Deasus/firestorm-firms-data'
FIRMS_JSON_PATH    = 'data/viirs.json'
GH_API_COMMITS_URL = f'https://api.github.com/repos/{FIRMS_REPO}/commits'
RAW_CONTENT_TMPL   = f'https://raw.githubusercontent.com/{FIRMS_REPO}/{{sha}}/{FIRMS_JSON_PATH}'

# 90-day trailing window matches UMD's documented algorithm.
LOOKBACK_DAYS = 90

# Sample cadence — we want ~1 sample per day. Cadence of the
# firestorm-firms-data auto-commits varies (skipped if diff-empty, plus
# workflow-fail gaps). Adaptive: divide total commits by lookback days to
# infer real cadence, then step by that. First-run observation 2026-07-02:
# 749 commits over 90 days = ~8/day (much lower than the 96/day 15-min
# cron implies — many cycles produce no diff and skip commit).
COMMITS_PER_SAMPLE_FLOOR = 1
COMMITS_PER_SAMPLE_ENV   = os.environ.get('COMMITS_PER_SAMPLE')

# Grid quantization at ~375m (VIIRS native pixel size at ~mid-latitude).
# 0.005° ≈ 550m at equator; 375m ≈ 0.0034°. Using 0.004° for a slight
# widening — matches VIIRS 375m pixel + 125m geolocation error budget.
GRID_DEG = 0.004

# ≥N distinct days over 90-day window = persistent. UMD's threshold isn't
# public; peer-reviewed VNF lit uses 5-10. We use 6 as a middle-of-consensus
# defensive floor. Tunable via env for experimentation.
MIN_DISTINCT_DAYS = int(os.environ.get('MIN_DISTINCT_DAYS', '6'))

# Default proximity buffer for the flag emission downstream.
BUFFER_M = 500

OUT_JSON = os.path.join('data', '_stage_firms_persistent.json')

HTTP_TIMEOUT = 30


def _http_json(url: str, headers: dict = None) -> Optional[dict]:
    """Fetch JSON with best-effort error handling. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers=headers or {
            'User-Agent': 'firestorm-industrial-sites/1.0',
            'Accept':     'application/json',
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except Exception as e:
        sys.stderr.write(f'[self_mine] fetch failed for {url}: {e}\n')
        return None


def sample_commit_shas() -> list[dict]:
    """Return the list of commit SHAs to sample from firestorm-firms-data
    over the LOOKBACK_DAYS window. GitHub API returns commits in reverse-
    chrono order; page through them until we exit the window.

    Returns: list of {sha, date} dicts sorted newest-first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cutoff_iso = cutoff.isoformat().replace('+00:00', 'Z')
    sys.stderr.write(f'[self_mine] sampling commits from {FIRMS_REPO} since {cutoff_iso}\n')

    # Anonymous GitHub API allows 60 req/hr per IP; we page 100 commits per
    # request. For 90 days × ~96 commits/day = ~8640 commits total → 87
    # pages. That's above the anonymous limit. Use the GITHUB_TOKEN env var
    # (auto-supplied on GHA runners) to boost to 5000 req/hr.
    headers = {
        'User-Agent': 'firestorm-industrial-sites/1.0',
        'Accept':     'application/vnd.github+json',
    }
    tok = os.environ.get('GITHUB_TOKEN')
    if tok:
        headers['Authorization'] = f'Bearer {tok}'
    else:
        sys.stderr.write('[self_mine] no GITHUB_TOKEN in env — anonymous 60 req/hr will likely 403\n')

    all_shas: list[dict] = []
    page = 1
    while page <= 100:   # hard ceiling — 10k commits at 100/page
        url = f'{GH_API_COMMITS_URL}?path={FIRMS_JSON_PATH}&per_page=100&since={cutoff_iso}&page={page}'
        commits = _http_json(url, headers=headers)
        if not commits or not isinstance(commits, list):
            break
        for c in commits:
            all_shas.append({
                'sha':  c.get('sha'),
                'date': c.get('commit', {}).get('committer', {}).get('date'),
            })
        if len(commits) < 100:
            break
        page += 1
    sys.stderr.write(f'[self_mine] found {len(all_shas)} commits in window (all pages walked)\n')

    # Adaptive sampling: aim for ~1 sample per day of the lookback window.
    # Env override wins for experimentation.
    if COMMITS_PER_SAMPLE_ENV:
        step = max(COMMITS_PER_SAMPLE_FLOOR, int(COMMITS_PER_SAMPLE_ENV))
    else:
        step = max(COMMITS_PER_SAMPLE_FLOOR, len(all_shas) // LOOKBACK_DAYS)
    # Sample every step-th commit — including newest (index 0) so we always
    # cover "today's" data.
    sampled = all_shas[::step]
    sys.stderr.write(f'[self_mine] sampling {len(sampled)} of {len(all_shas)} commits (every {step}th, adaptive from {LOOKBACK_DAYS}d target)\n')
    return sampled


def fetch_snapshot(sha: str) -> list[dict]:
    """Fetch one commit's viirs.json + return the detection rows.
    Returns [] on any error (e.g., early commit before viirs.json existed)."""
    url = RAW_CONTENT_TMPL.format(sha=sha)
    d = _http_json(url)
    if not d:
        return []
    return d.get('detections') or []


def build_persistent_index(samples: list[dict]) -> dict:
    """Grid-quantize + count distinct days per cell.
    Returns: {(lat_bin, lng_bin): {'days': set, 'first': str, 'last': str,
                                    'n_dets': int, 'sum_frp': float}}
    """
    grid = defaultdict(lambda: {'days': set(), 'first': '', 'last': '',
                                 'n_dets': 0, 'sum_frp': 0.0})
    for i, s in enumerate(samples):
        sha = s['sha']
        dets = fetch_snapshot(sha)
        if not dets:
            continue
        if i % 10 == 0:
            sys.stderr.write(f'[self_mine] processed {i}/{len(samples)} samples; '
                             f'{len(grid):,} cells so far\n')
        for det in dets:
            lat = det.get('lat')
            lng = det.get('lng')
            date = det.get('acq_date')
            if lat is None or lng is None or not date:
                continue
            # Grid bin — floor to GRID_DEG resolution
            key = (round(lat / GRID_DEG) * GRID_DEG,
                   round(lng / GRID_DEG) * GRID_DEG)
            cell = grid[key]
            cell['days'].add(date)
            cell['n_dets'] += 1
            cell['sum_frp'] += float(det.get('frp') or 0)
            if not cell['first'] or date < cell['first']:
                cell['first'] = date
            if not cell['last']  or date > cell['last']:
                cell['last']  = date
    return grid


def filter_persistent(grid: dict) -> list[dict]:
    """Keep only cells with ≥ MIN_DISTINCT_DAYS distinct days."""
    out = []
    for (lat, lng), cell in grid.items():
        if len(cell['days']) < MIN_DISTINCT_DAYS:
            continue
        out.append({
            'lat': round(lat, 5),
            'lng': round(lng, 5),
            'n_distinct_days': len(cell['days']),
            'n_detections':    cell['n_dets'],
            'first_seen':      cell['first'],
            'last_seen':       cell['last'],
            'mean_frp':        round(cell['sum_frp'] / cell['n_dets'], 1) if cell['n_dets'] else 0,
        })
    return out


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    samples = sample_commit_shas()
    if not samples:
        sys.stderr.write('[self_mine] no commit samples — nothing to mine\n')
        _write_empty(now_iso, reason='no_commits_in_window')
        return 0

    grid = build_persistent_index(samples)
    sys.stderr.write(f'[self_mine] indexed {len(grid):,} grid cells across '
                     f'{len(samples)} commit samples\n')

    persistent = filter_persistent(grid)
    sys.stderr.write(f'[self_mine] {len(persistent):,} persistent clusters '
                     f'(≥{MIN_DISTINCT_DAYS} distinct days over {LOOKBACK_DAYS}d)\n')

    payload = {
        'generated_utc':          now_iso,
        'source':                 'FIRESTORM self-mine of firestorm-firms-data 90-day archive',
        'algorithm':              f'{GRID_DEG:.4f}° grid quantization + ≥{MIN_DISTINCT_DAYS} distinct-day threshold',
        'lookback_days':          LOOKBACK_DAYS,
        'sampled_commits':        len(samples),
        'grid_cells_total':       len(grid),
        'min_distinct_days':      MIN_DISTINCT_DAYS,
        'grid_deg':               GRID_DEG,
        'count':                  len(persistent),
        'clusters':               persistent,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    os.replace(tmp, OUT_JSON)
    sys.stderr.write(f'[self_mine] wrote {OUT_JSON}\n')
    return 0


def _write_empty(now_iso: str, reason: str) -> None:
    payload = {
        'generated_utc':   now_iso,
        'source':          'FIRESTORM self-mine (empty)',
        'reason':          reason,
        'count':           0,
        'clusters':        [],
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))


if __name__ == '__main__':
    raise SystemExit(main())
