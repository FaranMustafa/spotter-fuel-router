# spotter-fuel-router

A Django 6.1 + Django REST Framework API that takes a start and a finish location in the
USA and returns the driving route, the cost-optimal fuel stops along it (500-mile range,
10 mpg, prices from the supplied OPIS list) and the total fuel cost. A request makes
**one** call to the free OSRM routing server, and **zero** when the route is cached. Start
and finish are resolved locally, the 8,023 stations are pre-geocoded in a committed
fixture, and the corridor search and planner run in memory with numpy. A Leaflet map
page draws the route and the numbered stops.

- Spec: [`docs/SPEC.md`](docs/SPEC.md) · Decisions and alternatives:
  [`docs/DECISIONS.md`](docs/DECISIONS.md) · Demo script:
  [`docs/LOOM_SCRIPT.md`](docs/LOOM_SCRIPT.md) · File index: [`filemap.md`](filemap.md)

## Quick start

Requires Python 3.12+. With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv sync                                        # installs runtime + dev deps from uv.lock
uv run python manage.py migrate
uv run python manage.py import_fuel_prices     # CSV + committed geocode fixture, no network
uv run python manage.py build_gazetteer        # recommended: one-time 634 KB GeoNames download
uv run python manage.py runserver
```

With pip:

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt            # or requirements.txt for runtime only
python manage.py migrate
python manage.py import_fuel_prices
python manage.py build_gazetteer
python manage.py runserver
```

**Why `build_gazetteer`?** Start and finish names are looked up first in the `Place`
table, which holds every city that has a truck stop (Chicago, Denver, Dallas, Miami, ...).
Big cities without a truck stop in the list, such as New York and Los Angeles, are found
in the GeoNames US postal-place file that this command downloads into `.cache/geonames/`
(gitignored). Without it those names fall back to Nominatim. That still works, but
NYC → LA then makes 3 external calls instead of 1, and 2 on a repeat, because Nominatim
lookups are not cached.

Try it:

```sh
curl -G http://127.0.0.1:8000/api/route/ \
  --data-urlencode "start=Chicago, IL" \
  --data-urlencode "finish=Denver, CO" \
  --data-urlencode "geometry=none"

curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Miami, FL", "finish": "Seattle, WA"}'
```

**Postman:** create a `GET` request to `http://127.0.0.1:8000/api/route/` and put
`start` and `finish` in the *Params* tab (Postman URL-encodes the comma and space). For
`POST`, choose *Body → raw → JSON* and send the same fields as a JSON object. Open the
`route.map_url` from any response in a browser to see the map. The requests used in the
demo are listed in [`docs/LOOM_SCRIPT.md`](docs/LOOM_SCRIPT.md#postman-collection).

## API

### `GET /api/route/` and `POST /api/route/`

GET takes query parameters. POST takes the same fields as a JSON body.

| Parameter | Type | Default | Range | Meaning |
|-----------|------|---------|-------|---------|
| `start` | string | required | ≤ 200 chars | `"City, ST"`, `"City ST"`, `"City, State Name"`, or `"lat,lon"` |
| `finish` | string | required | ≤ 200 chars | same formats as `start` |
| `corridor_miles` | float | `10` | 0.5–100 | max straight-line distance of a station from the route |
| `initial_fuel_gallons` | float | full tank | 0 to `range_miles / mpg` | fuel in the tank at the start |
| `mpg` | float | `10` | 1–100 | fuel economy |
| `range_miles` | float | `500` | 10–2000 | range on a full tank; tank size = `range_miles / mpg` |
| `min_stop_savings_usd` | float | `5` | 0–1000 | merge stops that save less than this into the previous stop; `0` gives the pure greedy optimum |
| `geometry` | string | `simplified` | `simplified`, `full`, `none` | route line in the response: Douglas–Peucker at 0.0005° (about 50 m), every OSRM vertex, or `null` |

Planning always uses the full OSRM geometry. `geometry` only changes what is sent back.

### `GET /api/route/map/`

Takes the same query parameters, except `geometry`. Renders a Leaflet page with the
route, start and finish markers, numbered stop markers (popups show name, price, gallons
and cost) and a summary panel. Errors are shown on the page with the same status code
the API would return.

### `GET /api/health/`

Returns `{"status": "ok"}`.

### Response (200)

A real response for `start=Chicago, IL&finish=Denver, CO&geometry=none` (first request,
so the route was not cached):

```json
{
  "start": {"query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.85421, "lon": -87.67605},
  "finish": {"query": "Denver, CO", "label": "Denver, CO", "lat": 39.73873, "lon": -104.98169},
  "route": {
    "distance_miles": 1003.2,
    "duration_hours": 17.79,
    "geometry": null,
    "provider": "osrm",
    "map_url": "http://127.0.0.1:8767/api/route/map/?start=Chicago%2C+IL&finish=Denver%2C+CO&geometry=none"
  },
  "fuel": {
    "mpg": 10.0,
    "range_miles": 500.0,
    "tank_gallons": 50.0,
    "initial_fuel_gallons": 50.0,
    "total_gallons": 52.06,
    "total_cost_usd": 146.63,
    "fuel_at_finish_gallons": 0.0,
    "min_stop_savings_usd": 5.0,
    "merged_stops": 0,
    "warnings": [],
    "stops": [
      {
        "order": 1,
        "station": {"id": 13417, "opis_id": 69840, "name": "KUM & GO #0370",
                    "address": "I-80, EXIT 439 & SR-370", "city": "Gretna", "state": "NE"},
        "lat": 41.1345, "lon": -96.2458,
        "mile_marker": 483.3,
        "detour_miles": 1.9,
        "price_per_gallon": 2.921,
        "gallons_bought": 7.53,
        "cost_usd": 21.99,
        "fuel_on_arrival_gallons": 1.28,
        "fuel_on_departure_gallons": 8.81
      },
      {
        "order": 2,
        "station": {"id": 12591, "opis_id": 68368, "name": "AKAL TRAVEL CENTER",
                    "address": "I-80 EX 360", "city": "Waco", "state": "NE"},
        "lat": 40.9198, "lon": -97.4534,
        "mile_marker": 557.9,
        "detour_miles": 6.8,
        "price_per_gallon": 2.799,
        "gallons_bought": 44.53,
        "cost_usd": 124.64,
        "fuel_on_arrival_gallons": 0.0,
        "fuel_on_departure_gallons": 44.53
      }
    ]
  },
  "meta": {
    "external_calls": 1,
    "cached_route": false,
    "candidate_stations": 265,
    "geometry_points": 0,
    "routing_ms": 1065.4,
    "compute_ms": 51.9
  }
}
```

The plan buys a few gallons at Gretna ($2.921) only to reach Waco, NE ($2.799), then
fills up there for the last 445 miles. The vehicle reaches Denver empty. Repeating the
same request returned `"external_calls": 0, "cached_route": true, "routing_ms": 0.7`.

Notes on the fields:

- `total_gallons` and `total_cost_usd` are sums of the rounded per-stop values, so they
  always match the stops to the cent.
- `lat`/`lon` of a stop are the centroid of its **city**, not the exact exit (see
  Assumptions). `mile_marker` is the stop's position along the route.
- `warnings` explains zero-stop trips and merged micro-stops.
- `meta.external_calls` counts real HTTP attempts made by this request (OSRM and
  Nominatim, retries included). `compute_ms` is everything except routing.

### Errors

Every error uses the same envelope:

```json
{"error": {"code": "unreachable",
           "message": "No fuel station within 100 miles after mile 1,815 (next candidate at mile 1,955)",
           "details": {"from_miles": 1814.6, "to_miles": 1955.1, "range_miles": 100.0,
                       "suggestion": "Increase corridor_miles or range_miles"}}}
```

(That is the real response for NYC → LA with `range_miles=100`.)

| Status | `code` | When |
|--------|--------|------|
| 400 | `invalid_request` | parameter validation failed; `details` maps each field to its messages |
| 400 | `invalid_input` | empty location, or latitude/longitude out of range |
| 400 | `not_found` | the geocoder found no such place |
| 400 | `outside_usa` | the location lies outside the continental US, Alaska and Hawaii boxes, or Nominatim says it is not in the US |
| 400 | `parse_error` | malformed JSON body (DRF) |
| 405 | `method_not_allowed` | e.g. `DELETE /api/route/` (DRF) |
| 422 | `no_route` | OSRM found no route (or rejected the points; `details.provider_code` holds OSRM's code) |
| 422 | `unreachable` | a gap longer than the range with no station in the corridor; `details` gives the gap |
| 502 | `geocoding_unavailable` | Nominatim failed (network, HTTP error, bad payload) |
| 502 | `routing_unavailable` | OSRM timed out or failed to connect (after one retry), or HTTP error |
| 502 | `routing_invalid_response` | OSRM returned something unparseable |
| 500 | `internal_error` | anything unexpected (logged with a traceback) |

The mapping lives in `fuel/api/errors.py` (`describe_exception`), shared by the JSON API
and the map page.

## How it works

```
 start, finish (text or lat,lon)
        │
        ▼
 1. resolve      geocoding.py   lat,lon → Place table → GeoNames index → Nominatim (fallback)
        │                       0 external calls in the common case
        ▼
 2. route        routing.py     OSRM /route/v1/driving, overview=full, GeoJSON
        │                       1 external call; 0 on a cache hit (24 h, keyed on 4-dp coords)
        ▼
 3. corridor     corridor.py    numpy: mile markers, 1-mile resampling, nearest sample,
        │                       segment projection → candidates within corridor_miles
        ▼
 4. planner      planner.py     greedy gas-station algorithm on the candidates
        │
        ▼
 5. merge        planner.py     fold stops that save < min_stop_savings_usd into the previous one
        │
        ▼
 6. response     serializers.py rounding, Douglas–Peucker geometry, map_url, meta
```

`fuel/services/trip.py` (`plan_trip`) runs steps 1–5 and owns the timings and the
external-call counter. The views only validate, call `plan_trip` and serialize.

1. **Resolve.** `resolve_location` accepts `lat,lon`, `"City, ST"`, `"City ST"` and
   `"City, State Name"`. City names are normalized (case, punctuation, `St.`→Saint,
   `Ft.`→Fort, `Mt.`→Mount). Lookups go to the `Place` table first, then the GeoNames US
   postal index (if `build_gazetteer` has been run), then Nominatim. Every result is
   checked against the US bounding boxes.
2. **Route.** One OSRM request with the full geometry (about 35,000 vertices for NYC →
   LA). It has a 10 s timeout and one retry on connection errors or timeouts. The result
   is cached in the Django cache for 24 h.
3. **Corridor.** All US stations live in numpy arrays loaded once per process. The route
   is resampled to at most 1 mile between samples, and stations outside the route's
   bounding box (plus the corridor) are dropped. For the rest, the nearest sample is
   found with an equirectangular distance that uses each sample's own latitude, in chunks
   so memory stays bounded. Each hit is then projected onto the two adjacent segments.
   That gives `detour_miles` (perpendicular distance) and `mile_marker` (position along
   the route), which is scaled onto OSRM's road distance.
4. **Planner.** This is the classic single-vehicle gas-station greedy. Coast on the
   initial fuel to the cheapest station in reach. At each stop, if a cheaper station is
   in reach, buy just enough to get there. Otherwise, if the finish is in reach, buy just
   enough to arrive empty. Otherwise fill up and go to the cheapest station in reach. A
   station's round-trip detour (`2 × detour_miles`) is burned on the way to it.
5. **Merge.** The greedy often tops up two gallons to save a few cents. A post-pass folds
   such a stop into the previous one when that costs less than `min_stop_savings_usd`
   ($5 by default) and the previous stop's tank can hold the fuel. The number removed is
   reported in `fuel.merged_stops` and in a warning.
6. **Response.** Values are rounded (miles 0.1, gallons and USD 0.01, price 0.001,
   degrees 5 dp). The geometry is simplified for the response only, and `map_url`
   carries the plan parameters.

## Assumptions

- **Full tank at the start** by default. The assignment gives a range but no starting
  fuel, and a truck usually leaves the yard fuelled. It also makes the result easy to
  check: no stop before mile ~500. Set `initial_fuel_gallons` to start lower, including 0.
- **Arrive empty.** The last stop buys exactly what is needed to reach the finish, so
  `total_cost_usd` is the cost of this trip only. Leftover starting fuel shows up in
  `fuel_at_finish_gallons` on trips that need no stop.
- **City-level station coordinates.** The CSV has highway-exit addresses ("I-80, EXIT 439
  & SR-370") and no coordinates. Each station sits at its city's centroid (GeoNames US
  postal places; Nominatim for the 84 Canadian cities).
- **Corridor of 10 miles** (`corridor_miles`). A station counts if its city centroid is
  within 10 straight-line miles of the route.
- **Detour = 2 × straight-line distance** from the route to the station (there and
  back), burned as extra fuel on the leg that reaches the station. This is approximate in
  both directions: roads are longer than straight lines, but the real exit is usually
  closer than the city centroid.
- **US-only stations.** The CSV also lists Canadian truck stops. They are imported but
  left out of the station index, so 7,403 US stations can be chosen.
- **Static prices.** Prices are the CSV's retail prices, loaded once. No live prices, and
  no taxes or discounts.
- 10 mpg and 500 miles (a 50-gallon tank) are the defaults. Both can be changed per
  request.

## Performance

Measured on a laptop against the public OSRM server, with `build_gazetteer` run:

| Trip | Miles | Stops | Fuel cost | External calls | Notes |
|------|-------|-------|-----------|----------------|-------|
| New York, NY → Los Angeles, CA | 2,799 | 13 (4 merged) | $717.31 | 1 | routing ~1.4 s (OSRM latency), compute ~250 ms |
| same request again | 2,799 | 13 | $717.31 | 0 | cached route, routing ~2 ms |
| Miami, FL → Seattle, WA | 3,306 | 20 (2 merged) | $876.25 | 1 | |
| Chicago, IL → Denver, CO | 1,003 | 2 | $146.63 | 1 | |
| Dallas, TX → Austin, TX | 194 | 0 | $0.00 | 1 | warning: reachable on the initial fuel |

- Nearly all the latency is the OSRM round trip. Corridor search takes about 16 ms on a
  3,000-mile route, and the whole local computation (corridor, planner, merge, station
  lookup, serialization) takes about 250 ms for NYC → LA.
- NYC → LA response size: 59 KB with the default simplified geometry (2,586 points),
  724 KB with `geometry=full`.
- `import_fuel_prices` takes about 2 s and needs no network.

## Data pipeline

| Command | Network | What it does |
|---------|---------|--------------|
| `import_fuel_prices [--csv PATH] [--fixture PATH]` | none | Loads `fuel/data/geocoded_places.json` into `Place`, then reads `fuel/data/fuel-prices.csv`: 8,151 rows, of which 128 duplicates (same OPIS id, city, state and price) are dropped, leaving 8,023 `FuelStation` rows linked to their place. Idempotent; reloads the in-memory station index. |
| `build_gazetteer [--force-download]` | 1 download | Downloads GeoNames `US.zip` (634 KB) to `.cache/geonames/` and builds the city/state → centroid index. Used by `geocode_places`, and at runtime for start/finish names. |
| `geocode_places [--limit N] [--no-nominatim] [--fixture PATH]` | yes | For every station city/state without a `Place`: GeoNames first, then Nominatim at 1 request/s. Writes `Place` rows and re-exports the fixture. |

**The fixture is committed**, so a grader needs no network to load data. The 3,898
unique city/state pairs in the CSV resolve to 3,897 places: 3,813 from GeoNames and 84
Canadian cities from Nominatim. One pair, Pass Lake, ON, is unresolved, so 6 stations
have no coordinates.

To regenerate the fixture from scratch, start from an empty database (delete
`db.sqlite3` and migrate again), then run:

```sh
uv run python manage.py import_fuel_prices --fixture no-such-file.json  # stations only, no places
uv run python manage.py geocode_places   # GeoNames (downloads if missing), then Nominatim at 1 req/s;
                                         # links stations and rewrites fuel/data/geocoded_places.json
```

`geocode_places` only handles pairs that have no `Place` yet, so on an already-loaded
database it does nothing and just re-exports the fixture.

## Testing

```sh
uv run pytest                                          # 160 tests, ~4 s
uv run ruff check . && uv run ruff format --check .    # lint + strict formatting
```

Tests never call real services: OSRM and Nominatim are mocked with `responses`.
Coverage by area:

- **planner** (23 tests): scenarios, gaps, micro-stop merging, a randomized property
  test, and a brute-force DP check. The greedy matches the exact optimum when there are
  no detours.
- **corridor** (11): mile markers, resampling, a synthetic route, and a brute-force
  comparison.
- **geocoding** (18) and **routing** (11): input formats, US boxes, Nominatim, the call
  counter, cache hits, `NoRoute`, timeout retry, malformed JSON.
- **gazetteer** (12) and **data pipeline** (6): normalization, dedupe, cleaning, fixture
  load, idempotency.
- **geometry** (6), **trip** (5), **API** (19, including the map page), and a health
  smoke test.

## Limitations and next steps

- **OSRM demo server has no SLA** and a fair-use policy, so it is fine for a demo but not
  for production. Set `OSRM_BASE_URL` to a self-hosted OSRM (the US extract fits on one
  machine), or put another provider behind `routing.get_route`.
- **City-level coordinates.** Detours and stop markers are approximate. The upgrade is to
  geocode exits by matching the `I-xx EXIT nnn` pattern against OSM motorway-junction
  nodes, or to use a commercial truck-stop dataset with coordinates.
- **Greedy + merge is not the exact optimum** once detours and the per-stop merge
  threshold are involved. On random small instances with detours, the greedy averaged
  about $0.65 above the true optimum. The merge also cannot remove a top-up when the
  previous stop already left with a full tank. The upgrade is an exact DP over (station,
  discretized fuel) with an explicit per-stop penalty. It is small enough here (a few
  hundred candidates), but it needs care to stay fast in Python.
- **Nominatim fallback.** It is used only when a start or finish is not in the local
  gazetteer. It sends a custom User-Agent and makes at most two calls per request, but it
  is not cached or rate limited at runtime. A heavier deployment should cache it, or
  drop it in favour of the local gazetteer plus `lat,lon` input. Its `countrycodes=us`
  filter can also turn a foreign name into a US place (for example, `"Toronto, ON"`
  resolves to a location in Indianapolis). Rejecting non-US region codes before calling
  Nominatim would fix that.
- **No auth or rate limiting**, and the cache is per-process `LocMemCache`. Production
  would add API keys or throttling (DRF throttles), Redis for a shared route cache,
  Postgres, and gunicorn.
- **Static prices.** A real system would refresh prices on a schedule, and the station
  index would reload with them (`reload_station_index()` already exists).
