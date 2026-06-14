# Agri-Travel-Logistics Platform

An enterprise platform that bridges **agriculture** (weather, Growing Degree
Days, soil monitoring, drone surveys), **travel / routing** (hazard-aware vehicle
routing) and a **logistics marketplace** (post loads, bid rates, AI-driven
culturally-aware haggling). It turns continuous climate data, discrete hazard
intelligence and a live carrier marketplace into safe, cost-aware agricultural
supply chains.

```
   ┌──────────────┐   ┌──────────────┐   ┌─────────────────────────┐
   │    WFAAS      │   │    TRAAS     │   │  LGaaS  (prisaMove)     │
   │ weather +     │   │ routing +    │   │  loads · vehicles ·     │
   │ agronomy +    │   │ hazards +    │   │  offers · pricing ·     │
   │ drones        │   │ safety/cost  │   │  AI haggling            │
   └──────┬───────┘   └──────┬───────┘   └───────────┬─────────────┘
          │      agri_platform.common (config · logging · auth ·     │
          │      rate-limit · cache · errors · pagination · geo ·    │
          │      GDD · weather client · db)                          │
          └─────────────────────────┬───────────────────────────────┘
                              ┌──────┴───────┐
                              │   Frontend   │  MapLibre GIS dashboard + prisaMove board
                              └──────────────┘
  Data: Open-Meteo · Valhalla/OSRM · PostGIS · Redis · OSM tiles
```

## Services

| Service | Brand | Port | Responsibility |
|---------|-------|------|----------------|
| `agri_platform/wfaas` (WFAAS) | **prisaForecast** | 5001 | Weather forecasts, GDD, soil/weather alerts, drone missions + AI imagery analysis, background monitoring + notifications |
| `agri_platform/traas` (TRAAS) | **prisaTravel** | 5002 | Hazard-aware routing, hazard zones, safety scoring, transit cost |
| `agri_platform/marketplace` (LGaaS) | **prisaMove** | 5003 | Loads, vehicles, offers/bids, fair-rate pricing, AI culturally-aware haggling |

The `prisa*` names are the customer-facing brands; each service's `/health`
returns both its internal `service` id and its `brand`.

Each service is independently deployable, owns its own database, and is built on
the shared `agri_platform.common` core. Domain logic (geo math, GDD, pricing,
negotiation strategy, hazard scoring) is pure Python — fully unit-testable with
no network or database.

## Enterprise capabilities

- **Config** — every tunable read once into an immutable `Settings` (`common/config.py`).
- **Security** — API-key auth (`X-API-Key` / `Bearer`), per-key/IP **rate limiting**.
- **Observability** — structured JSON logging with per-request correlation IDs; `/health` + `/readyz`.
- **Consistent errors** — single JSON envelope `{"error":{"code","message","details"}}`.
- **Caching** — pluggable cache (in-memory default, Redis when `REDIS_URL` is set); weather responses cached.
- **Pagination** on all list endpoints (`?page=&page_size=`).
- **Migrations** — Alembic per service (generated, verified in CI).
- **Notifications** — SMTP engine (console fallback) honouring per-farm preferences.
- **Background jobs** — APScheduler farm-hazard scanner (`MONITOR_ENABLED`).
- **CI** — GitHub Actions: tests on 3.10–3.12 + migration apply check.

## Quick start (local, zero infrastructure)

Services default to a local SQLite DB; TRAAS uses an offline straight-line
routing fallback; caches and AI integrations degrade gracefully. So everything
runs with nothing else installed.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

make run-wfaas      # http://localhost:5001
make run-traas      # http://localhost:5002
make run-lgaas      # http://localhost:5003  (prisaMove)
make frontend       # http://localhost:8080  (dashboard + marketplace board)

make seed           # optional demo data
```

## Full stack (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Brings up the three services on Postgres/PostGIS, Redis, a Valhalla routing
engine, and the frontend on <http://localhost:8080>. Drop a regional OSM extract
(e.g. `nigeria-latest.osm.pbf`) into `./valhalla_data/` for real road routing.

## Database migrations

```bash
make migrate        # all three, or individually:
alembic -c alembic-wfaas.ini upgrade head
alembic -c alembic-traas.ini upgrade head
alembic -c alembic-lgaas.ini upgrade head
```

The URL comes from `$DATABASE_URL` (SQLite locally, Postgres in Docker).

## API reference

### WFAAS / prisaForecast — `:5001`
`GET /health` · `GET /readyz` · farms (`POST/GET /api/farms`, `GET /api/farms/{id}`,
`POST /api/farms/{id}/drones`) · drones (`POST/GET /api/drones`,
`POST /api/drones/missions`, `PUT /api/missions/{id}/result`,
`POST /api/missions/{id}/analyze`) · weather (`POST /api/weather/forecast|daily`,
`POST /api/agronomy/gdd`) · alerts (`POST /api/alerts/detect`, `GET /api/alerts`,
`POST /api/alerts/{id}/resolve`, `POST/GET /api/alerts/preferences`) ·
`POST /api/monitoring/scan`.

### TRAAS / prisaTravel — `:5002`
`GET /health` · `GET /readyz` · `POST /api/routes/safe` ·
`POST /api/routes/optimize` (avoids active DB hazards; returns safety + cost) ·
`POST /api/routes/analyze` · `POST /api/routes/distance` · `GET /api/routes` ·
hazards (`POST/GET /api/hazards`, `DELETE /api/hazards/{id}`) ·
`POST/GET /api/constraints`.

### LGaaS / prisaMove — `:5003`

| Method & path | Purpose |
|---------------|---------|
| `POST /api/loads` · `GET /api/loads` · `GET /api/loads/{ref}` | post / browse loads |
| `POST /api/loads/{ref}/status` | update lifecycle status |
| `POST /api/loads/{ref}/estimate` · `POST /api/estimate` | fair-rate quote (persisted / ad-hoc) |
| `POST /api/vehicles` · `GET /api/vehicles` | register / list vehicles |
| `POST /api/loads/{ref}/offers` · `GET /api/loads/{ref}/offers` | place / list bids (with capacity & handling checks) |
| `POST /api/offers/{id}/counter` · `/accept` · `/reject` | negotiate / award |
| `POST /api/offers/{id}/haggle` | **AI culturally-aware counter-offer suggestion** |
| `GET /api/loads/{ref}/messages` | negotiation thread |
| `GET /api/negotiation-styles` | available negotiation-style profiles |
| `POST /api/tax/quote` | compute statutory tax for an amount (admin-configured rules) |
| `/api/admin/tax-rules*` | **admin-only** tax/VAT/levy rule management (`X-Admin-Key`) |

```bash
# Post a load, get a fair quote, bid, then ask the AI to haggle as the shipper
curl -s localhost:5003/api/loads -H 'content-type: application/json' -d '{
  "shipper_id":"farmer-1","title":"Tomatoes","weight_kg":8000,
  "load_type":"perishable_produce","classifications":["refrigerated"],
  "origin":{"lat":10.5,"lon":7.4},"destination":{"lat":11.8,"lon":13.1},"budget":900}'

curl -s localhost:5003/api/offers/1/haggle -H 'content-type: application/json' \
  -d '{"as_role":"shipper","negotiation_style":"market_bargaining","counterparty_name":"Transporter A"}'
```

## How it works

**Hazard-aware routing (TRAAS).** Hazards are stored as buffered points or GeoJSON
polygons; on `optimize` they become routing `exclude_polygons`, the route is
scored (each hazard class erodes a 0–1 safety score → `low|medium|high|critical`),
and transit cost is estimated per vehicle class.

**Fair-rate pricing (LGaaS).** Quotes use *chargeable weight* (max of actual and
volumetric weight), road distance, a load-type multiplier, additive surcharges
for special handling (refrigerated, hazardous, fragile, oversized, livestock) and
an urgency factor, returning a recommended price with a low/high band.

**AI haggling with cultural nuance (LGaaS).** A deterministic concession strategy
decides *accept vs. counter* and computes a counter price; a pluggable composer
phrases it. "Cultural nuance" is modelled as **selectable negotiation-style
profiles** — tunable conventions (formality, relationship emphasis, directness,
bargaining intensity, greetings) such as `direct`, `relationship_first`,
`high_context_formal`, `market_bargaining`, `consensus`. These are *configurable
presets describing negotiation styles* (and you can pass a fully custom profile);
they are not assumptions about any individual. The default composer is offline
and deterministic; set `HAGGLE_AI_ENDPOINT` to delegate phrasing to an LLM
service (it falls back to the template composer on any error). No analysis or
message is fabricated when an endpoint is absent.

## Africa-centric regions & i18n

The platform defaults to African markets and localizes **automatically** across
all three services.

- **Region registry** (`common/regions.py`) — ~20 African markets, each with
  spoken languages (primary first), settlement currency, timezone and an
  approximate bounding box. `region_for_point(lat, lon)` infers the market from
  coordinates (falls back to a pan-African default).
- **i18n** (`common/i18n.py`) — fallback-safe translation (`requested → base →
  English`) across **English, French, Portuguese, Swahili, Arabic, Hausa**.
  Partial catalogs never break or fabricate output — they fall back. New
  languages/keys are pure data.
- **Auto localization** —
  - Language per request from `?lang=` or the `Accept-Language` header; responses
    carry `Content-Language`.
  - **LGaaS** infers a load's **currency** from its origin region (e.g. Senegal →
    XOF, Kenya → KES, Nigeria → NGN) unless a currency is given, and the AI
    haggling messages are emitted in the region's language (or an explicit
    `lang`). Example: a Kenya load auto-produces Swahili counter-offers.
- **Discovery endpoints** (on every service): `GET /api/i18n/languages`,
  `GET /api/regions`, `GET /api/regions/lookup?lat=&lon=`.

```bash
curl "localhost:5003/api/regions/lookup?lat=-1.29&lon=36.82"   # -> Kenya, sw, KES
curl -s localhost:5003/api/offers/1/haggle -H 'content-type: application/json' \
  -d '{"as_role":"shipper","lang":"fr"}'                        # French counter-offer
```

> The negotiation-style profiles and language catalogs are configurable
> conventions, not assumptions about individuals; extend or override them freely.

## Tax / VAT / statutory payments (admin-managed)

Tax is computed by an engine (`common/tax.py`) that applies **operator-configured
rules** — the platform **ships with no rates**. Statutory rates differ by
jurisdiction and change by law, so an administrator enters and maintains the real
rates; until then the computed tax is zero and the response says `configured:
false` (never a guessed rate). The maths is exact `Decimal` arithmetic, fully
tested — a real calculation, not a simulation.

- **Collection modes**: `add` (VAT/GST/levies added to the buyer's total) and
  `withhold` (withholding tax deducted from the carrier's payout and remitted).
- **Basis**: `net` or `compound` (tax-on-tax) where a jurisdiction requires it.
- **Rule fields**: region (ISO-2 or `*`), rate, type, applicability (categories),
  threshold, sequence, effective-from/to dates, and a `statutory_reference` field
  for the legal basis (audit).

**Admin API** (`/api/admin/tax-rules`, LGaaS) is protected by a dedicated
`X-Admin-Key` and is **disabled until `ADMIN_API_KEYS` is set** (it is never
left open):

| Method & path | Purpose |
|---------------|---------|
| `POST /api/admin/tax-rules` | create a rule |
| `GET /api/admin/tax-rules` · `GET /api/admin/tax-rules/{code}` | list / fetch |
| `PUT /api/admin/tax-rules/{code}` | update |
| `DELETE /api/admin/tax-rules/{code}` | disable (`?hard=true` to remove) |
| `POST /api/tax/quote` | compute tax for an amount (public) |

Configured taxes are automatically included in `POST /api/loads/{ref}/estimate`
and `POST /api/estimate` responses for the load's region. The admin UI is
`frontend/admin.html`.

```bash
# Enable admin, add a real (operator-verified) VAT rule, then quote
export ADMIN_API_KEYS=changeme
curl -s localhost:5003/api/admin/tax-rules -H "X-Admin-Key: changeme" \
  -H 'content-type: application/json' -d '{
    "code":"NG-VAT","region_code":"NG","name":"VAT","tax_type":"vat",
    "collection":"add","rate_percent":7.5,"applies_to":["transport_service"],
    "statutory_reference":"operator-supplied"}'
curl -s localhost:5003/api/tax/quote -H 'content-type: application/json' \
  -d '{"base_amount":1000,"region_code":"NG","currency":"NGN"}'
```

## Configuration

| Env var | Default | Used by |
|---------|---------|---------|
| `DATABASE_URL` | `sqlite:///<service>.db` | all |
| `REDIS_URL` | _unset → in-memory cache_ | wfaas, lgaas |
| `AUTH_ENABLED` / `API_KEYS` | `false` / _empty_ | all |
| `ADMIN_API_KEYS` | _empty → admin disabled_ | lgaas (tax admin) |
| `RATE_LIMIT_PER_MINUTE` | `0` (off) | all |
| `VALHALLA_URL` | _unset → straight-line_ | traas |
| `OPEN_METEO_URL` | Open-Meteo public API | wfaas |
| `DEFAULT_LANGUAGE` / `DEFAULT_REGION` | `en` / `NG` | all (i18n fallback) |
| `MONITOR_ENABLED` / `MONITOR_INTERVAL_MINUTES` | `false` / `30` | wfaas |
| `NOTIFICATIONS_ENABLED` + `SMTP_*` | `false` | wfaas |
| `DRONE_AI_ENDPOINT` | _unset_ | wfaas (drone imagery) |
| `HAGGLE_AI_ENDPOINT` | _unset_ | lgaas (haggle phrasing) |

## Testing

```bash
make test          # pytest -q
```

Covers the pure helpers (geo, GDD, pricing, negotiation strategy) and the full
HTTP surface of all three services using in-memory SQLite and fake weather /
routing / inference collaborators — no network required.

## Project layout

```
agri_platform/
  common/        config, logging, errors, security, ratelimit, cache, pagination,
                 geo, weather, db, regions, i18n, localization, tax
  wfaas/         models, service, notifications, monitoring, ai, app, wsgi, Dockerfile
  traas/         models, routing, service, app, wsgi, Dockerfile
  marketplace/   models, pricing, negotiation, service, app, wsgi, Dockerfile   (LGaaS / prisaMove)
migrations/      Alembic envs for wfaas / traas / lgaas
frontend/        index.html (GIS dashboard) + marketplace.html (prisaMove)
tests/           pytest suite
manage.py        ops CLI (init-db, seed, scan)
```
