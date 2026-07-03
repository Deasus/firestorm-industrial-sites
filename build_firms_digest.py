#!/usr/bin/env python3
"""Weekly FIRMS-validation digest.

Reads operator verdicts from the private Deasus/firestorm-deconflict-feedback
GitHub Issues datastore (populated in real-time by the FIRESTORM frontend via
the Vercel /api/deconflict-feedback route), aggregates the last 7 days, and
emits a Markdown-formatted digest suitable for emailing to the NASA FIRMS
validation team.

Per recon 2026-07-02: NASA has NO formal validation-data intake channel. The
practical path is email to earthdata-support@nasa.gov (subject "FIRMS:
Operator Validation Data Submission") CC Louis Giglio (UMD MODIS Fire team)
+ Wilfrid Schroeder (NOAA NESDIS). No operator platform currently does this
— FIRESTORM would be the first.

This script writes the digest to data/firms_digest_YYYYWWW.md in the
firestorm-industrial-sites repo. A separate GHA cron step (or a human)
attaches the file to the outbound email — we don't automate SMTP because
the email needs to come from a real DOI/USGS address for professional
credibility, not a service-account bot.
"""
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

FEEDBACK_REPO = 'Deasus/firestorm-deconflict-feedback'
LOOKBACK_DAYS = 7
OUT_DIR       = 'data'
HTTP_TIMEOUT  = 30

VERDICT_LABELS = {
    'real_fire':  'REAL FIRE (operator overrode the pipeline flag)',
    'industrial': 'KNOWN INDUSTRIAL (operator confirmed flag)',
    'volcano':    'KNOWN VOLCANO (operator confirmed flag)',
    'offshore':   'OFFSHORE (operator confirmed sun-glint / ship / offshore platform)',
    'unsure':     'UNSURE (operator flagged for later review)',
}


def _http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp), resp.headers


def fetch_recent_issues(since_iso, token):
    headers = {
        'Authorization':          f'Bearer {token}',
        'Accept':                 'application/vnd.github+json',
        'X-GitHub-Api-Version':   '2022-11-28',
        'User-Agent':             'firestorm-industrial-sites/firms-digest/1.0',
    }
    out = []
    page = 1
    while page < 50:
        url = (f'https://api.github.com/repos/{FEEDBACK_REPO}/issues'
               f'?state=all&per_page=100&since={since_iso}&page={page}')
        issues, hdrs = _http_json(url, headers=headers)
        if not issues:
            break
        issues = [i for i in issues if 'pull_request' not in i]
        out.extend(issues)
        link = hdrs.get('Link', '')
        if 'rel="next"' not in link:
            break
        page += 1
    return out


_RECORD_RE = re.compile(r'```json\s*(\{.*?\})\s*```', re.DOTALL)


def parse_verdict(issue):
    body = issue.get('body') or ''
    m = _RECORD_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def build_digest(records, since_iso, until_iso):
    lines = []
    lines.append('# FIRESTORM Operator Validation Digest')
    lines.append('')
    lines.append(f'**Window:** {since_iso} → {until_iso}')
    lines.append(f'**Total operator verdicts:** {len(records)}')
    lines.append('')
    lines.append('## Provenance')
    lines.append('')
    lines.append('Every verdict below was submitted by a wildfire operator using')
    lines.append('FIRESTORM (federal-facing wildfire common-operating-picture).')
    lines.append('Operators tap one of five verdict buttons on a chip glyph shown')
    lines.append('next to a NASA FIRMS VIIRS/MODIS detection that FIRESTORM has')
    lines.append('flagged as a probable non-fire (industrial thermal source /')
    lines.append('volcano / nuclear plant / persistent thermal signature / low')
    lines.append('confidence). Deconfliction reference layer: EPA Facility')
    lines.append('Registry Service filtered to thermal NAICS (~9600 facilities),')
    lines.append('Smithsonian Global Volcanism Program (~1200 volcanoes), NASA')
    lines.append('GIBS nuclear plants (235), and a self-mined persistent-source')
    lines.append('layer from our own 90-day FIRMS archive (~1100 clusters).')
    lines.append('')
    lines.append('## Contact')
    lines.append('')
    lines.append('- Operator platform: FIRESTORM — <https://deasus.github.io/Firestorm/>')
    lines.append('- Source: <https://github.com/Deasus/firestorm-industrial-sites>')
    lines.append('- Point of contact: Deepinder Uppal (duppal@ios.doi.gov), DOI OCIO CTO')
    lines.append('')

    if not records:
        lines.append('_(No operator verdicts recorded this week.)_')
        return '\n'.join(lines)

    verdict_counts = Counter(r.get('verdict', 'unknown') for r in records)
    lines.append('## Aggregate verdict distribution')
    lines.append('')
    lines.append('| Verdict | Count | Meaning |')
    lines.append('|---|---|---|')
    for v in ('real_fire', 'industrial', 'volcano', 'offshore', 'unsure'):
        n = verdict_counts.get(v, 0)
        if n > 0:
            lines.append(f'| `{v}` | {n} | {VERDICT_LABELS.get(v, v)} |')
    lines.append('')

    real_fire = [r for r in records if r.get('verdict') == 'real_fire']
    if real_fire:
        by_flag = Counter(r.get('pipeline_flag') or 'unknown' for r in real_fire)
        lines.append('## Pipeline flags OVERRIDDEN as real fires (operator disagreement)')
        lines.append('')
        lines.append('These flags were caught by FIRESTORM as likely non-fires, but the')
        lines.append('operator marked the detection as a real wildfire. **This is the**')
        lines.append('**most actionable signal for FIRMS algorithm improvement.**')
        lines.append('')
        lines.append('| Pipeline flag | Overrides | Suggests |')
        lines.append('|---|---|---|')
        for flag, n in by_flag.most_common():
            hint = {
                'proximate_industrial':       'Real fire at an industrial site (refinery / kiln explosion / flare-adjacent brush)',
                'proximate_volcano':          'Real fire on volcanic flank / lava-caused ignition',
                'proximate_nuclear':          'Real fire near a nuclear plant (rare — investigate closely)',
                'proximate_persistent_other': 'Persistent-source site fired at fire intensity — worth updating classification',
                'low_confidence':             'VIIRS confidence=low was correct — real fire NASA drops',
            }.get(flag, 'Unknown flag class')
            lines.append(f'| `{flag}` | {n} | {hint} |')
        lines.append('')

        lines.append('### Real-fire override detail (up to 50 records)')
        lines.append('')
        lines.append('| Date | Lat | Lng | Sensor | Flag | Nearest source | FRP (MW) |')
        lines.append('|---|---|---|---|---|---|---|')
        for r in real_fire[:50]:
            lines.append(
                '| {date} | {lat:.4f} | {lng:.4f} | {sensor} | `{flag}` | {near} | {frp} |'.format(
                    date=r.get('acq_date','?'),
                    lat=r.get('lat',0),
                    lng=r.get('lng',0),
                    sensor=r.get('sensor','?'),
                    flag=r.get('pipeline_flag','?'),
                    near=(r.get('nearest_infra') or '—')[:60],
                    frp=r.get('frp') or '?',
                )
            )
        lines.append('')

    confirmed = [r for r in records if r.get('verdict') in ('industrial','volcano','offshore')]
    if confirmed:
        by_flag = defaultdict(int)
        for r in confirmed:
            key = f'{r.get("pipeline_flag","?")} → verdict:{r.get("verdict")}'
            by_flag[key] += 1
        lines.append('## Confirmed flags (operator agreed with pipeline)')
        lines.append('')
        lines.append('| Pipeline flag → verdict | Count |')
        lines.append('|---|---|')
        for k, n in sorted(by_flag.items(), key=lambda x: -x[1]):
            lines.append(f'| `{k}` | {n} |')
        lines.append('')

    lines.append('---')
    lines.append(f'*Digest generated {datetime.now(timezone.utc).isoformat()} '
                 f'by firestorm-industrial-sites/build_firms_digest.py.*')
    return '\n'.join(lines)


def main() -> int:
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GITHUB_FEEDBACK_TOKEN')
    if not token:
        sys.exit('[digest] GITHUB_TOKEN not set — cannot read private feedback repo')

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    since_iso = since.strftime('%Y-%m-%dT%H:%M:%SZ')
    now_iso   = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    sys.stderr.write(f'[digest] fetching verdicts from {FEEDBACK_REPO} since {since_iso}\n')
    try:
        issues = fetch_recent_issues(since_iso, token)
    except Exception as e:
        sys.stderr.write(f'[digest] fetch failed: {e} — emitting empty digest\n')
        issues = []

    records = [r for r in (parse_verdict(i) for i in issues) if r]
    sys.stderr.write(f'[digest] parsed {len(records)} verdicts from {len(issues)} issues\n')

    md = build_digest(records, since_iso, now_iso)

    os.makedirs(OUT_DIR, exist_ok=True)
    iso_week = now.strftime('%Y-W%V')
    out_path = os.path.join(OUT_DIR, f'firms_digest_{iso_week}.md')
    with open(out_path, 'w') as f:
        f.write(md + '\n')
    sys.stderr.write(f'[digest] wrote {out_path}\n')

    latest_path = os.path.join(OUT_DIR, 'firms_digest_latest.md')
    with open(latest_path, 'w') as f:
        f.write(md + '\n')
    sys.stderr.write(f'[digest] wrote {latest_path}\n')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
