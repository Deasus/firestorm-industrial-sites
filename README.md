# firestorm-industrial-sites

Known-thermal-source deconfliction layer for FIRESTORM. Publishes a slim
`industrial_sites.geojson` the FIRMS pipeline uses to filter false-positive
thermal anomalies (VIIRS/MODIS hits that are actually refineries, power
plants, cement kilns, volcanoes, or ocean sun-glint — not fires).

## Why this exists

NASA FIRMS' near-real-time feeds strip out NASA's own `type` classification
field (0=vegetation fire, 1=volcano, 2=static industrial, 3=offshore) — it
only exists in the monthly archive product. So downstream users (us) have
to rebuild the deconfliction layer ourselves against public reference data.

Operators repeatedly see VIIRS/MODIS thermal anomalies that aren't fires:
- Petroleum refineries (permanent 350K+ IR signature)
- Cement kilns (1400-1500°C rotary kilns)
- Fossil-fuel power plants (stack thermal)
- Steel mills (arc furnaces, blast furnaces)
- Waste combustion / municipal solid waste incinerators
- Active volcanoes (Kilauea, Great Sitkin, Semisopochnoi, ...)
- Offshore sun glint / ship traffic

This pipeline aggregates authoritative public datasets into a single
GeoJSON `firestorm-firms-data` reads at ingest to compute
`nearest_infra_m` + `deconfliction_flag` on every detection.

## Sources (MVP)

| Source | What it covers | Refresh |
|---|---|---|
| EPA Facility Registry Service (bulk) | US industrial thermal emitters, filtered to NAICS `324110` (petroleum refineries), `221112` (fossil-fuel power), `331110` (steel mills), `327310` (cement), `562213` (waste combustion) | monthly (EPA bulk publish cadence) |
| Smithsonian Global Volcanism Program | ~1,350 Holocene volcanoes globally | weekly (WFS GetFeature) |
| Natural Earth 10m land | Offshore-proxy polygon for sun-glint / ship / flare-platform suppression | quarterly (dataset is stable) |

## Output

`data/industrial_sites.geojson` — FeatureCollection of Points with:

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [-102.3, 31.7] },
  "properties": {
    "source_id": "epa_frs:110000123456",
    "source_type": "petroleum_refinery",
    "class": "industrial",
    "name": "Example Refinery",
    "state": "TX",
    "buffer_m": 500,
    "provenance": "EPA FRS 2026-Q2"
  }
}
```

`class` is one of: `industrial`, `volcano`, `offshore_proxy` (Natural Earth
land polygon lives in a separate file — see below).

`buffer_m` is the per-class default distance around this source within which
a VIIRS/MODIS detection is tagged as proximate:
- industrial: 500m
- volcano: 5000m (GVP points are volcano summits; vents/lava can be km away)
- solar (v2): 375m

`data/industrial_sites_meta.json` — refresh manifest (per-source last-fetch
timestamps, feature counts, error state).

`data/land_10m.geojson` — Natural Earth 10m land polygons for offshore
detection (kept separate from point sources — polygon ops are the
distance-to-land test, not a nearest-point test).

## Cadence

Weekly GHA cron, Sundays 08:00 UTC. All sources are slow-changing (facility
permitting timelines are months; volcano catalog updates are episodic).

Manual re-run via workflow_dispatch if a specific source needs an urgent
refresh (e.g. new Great Sitkin eruption listed in GVP).

## Consumers

- `firestorm-firms-data/fetch_firms.py` — reads `industrial_sites.geojson`
  at ingest, computes `nearest_infra_m` + `deconfliction_flag` on each
  VIIRS/MODIS detection via BallTree(haversine).
- `firestorm/index.html` — reads for the operator feedback tooltip's
  "why this was flagged" content (source name + class + provenance).

## Non-goals (deferred to v2/v3)

- USGS USPVDB solar-array polygons (v2)
- Texas RRC + California CalGEM oilfield wells (v2)
- World Bank Global Gas Flaring Tracker (v2)
- Self-healing persistent-hotspot mining from our own FIRMS archive (v3)
- NASA VNP14IMGML archive reconstruction of the type-2 static-source mask (final fix, deferred)

See the FIRESTORM deconfliction MVP plan in the operator memo for full
tier scope.

## Auth

None. All sources are public-domain / open-data.

## License

Data derivatives inherit from source: EPA FRS is US-federal public domain,
GVP is Smithsonian public domain, Natural Earth is public domain. Aggregated
output is public domain.
