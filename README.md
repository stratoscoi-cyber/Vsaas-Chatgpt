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
| `GET /api/holidays` · `GET /api/calendar/business-day` | public-holiday & business-day calendar |
| `/api/admin/holidays*` | **admin-only** public-holiday management (`X-Admin-Key`) |
| `GET /api/regions/{code}/profile` | region cultural profile (currency, weekend, style, holidays) |
| `/api/shipments/*` · `/api/tracking/reasons` | live shipment tracking, ETA, delays & reroutes |
| `/api/drivers/*` · `/api/vehicles/*` · `/api/service-centers/*` · `/api/compliance/*` | onboarding & compliance |
| `GET /api/loads/{ref}/match-vehicles` · `/api/consolidations*` | FTL matching & LTL consolidation |

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

### Currency, public holidays & cultural nuance

- **Currency localization** (`common/currency.py`) — ISO 4217 reference data
  (symbol, minor units, placement) for every region's currency, formatted per
  locale (e.g. `₦1,234.50`, `1 000 CFA`, `$1 234,50` in French). Used in haggling
  messages, quotes and tax breakdowns. Endpoints (all services):
  `GET /api/i18n/currencies`, `GET /api/i18n/format?amount=&currency=`.
- **Public holidays** (`common/holidays.py` + LGaaS) — two real sources, no
  fabrication: the operator's admin table **and** the maintained
  [`python-holidays`](https://pypi.org/project/holidays/) library
  (`HOLIDAY_PROVIDER=library`), which ships real national calendars including
  movable dates (e.g. Eid). Admin entries always win on a date clash. The engine
  does the calendar maths (is-holiday, business-day, next-business-day,
  add-business-days) honouring each region's weekend.
  - Admin: `/api/admin/holidays` (CRUD, `X-Admin-Key`).
  - Public: `GET /api/holidays?region=&year=`,
    `GET /api/calendar/business-day?region=&date=&add=`.
  - Loads with a `delivery_deadline` automatically get a `schedule` block
    (is-holiday / next business day).
- **Cultural nuance** — each region carries a **weekend** (e.g. Egypt Fri–Sat vs
  Sat–Sun elsewhere) and a default **negotiation-style** preset.
  `GET /api/regions/{code}/profile` returns the region, currency, weekend,
  negotiation profile and upcoming holidays.

## prisaMove onboarding & compliance (anti-collusion)

Carriers, vehicles and service centres pass an automated **pre-registration check**
before they can transact. A deterministic rules engine (`compliance.py`) decides
**approved / rejected / review** from operator-configured requirements and
submitted evidence — humans only touch the genuinely suspicious cases.

- **Per-region requirements** (`/api/admin/region-rules`, admin) — required driver
  and vehicle documents, minimum driving experience (e.g. 2 years with proof),
  minimum insured value, inspection interval, tracker/camera requirement.
- **Approved agencies** (`/api/admin/agencies`, admin) — only documents issued by
  recognised licensing/insurance/inspection bodies count.
- **Evidence**: documents (DL, permits, registration, insurance with insured value
  + coverage), and periodic physical/mechanical **inspections** recorded by a
  service centre. A document only passes when present, issued by an approved
  agency, unexpired and verified — nothing is assumed.
- **IoT/camera**: a vehicle must have an approved, serviceable tracker and onboard
  camera when the region requires them.
- **Anti-collusion signals** downgrade an otherwise-approved entity to `review`:
  self-inspection (vehicle inspected by a centre owned by the vehicle's owner),
  re-used documents (shared `doc_hash`), abnormal inspection velocity, and owners
  with repeated rejections. `POST /api/admin/compliance/decisions/{id}/override`
  records an auditable human decision.
- **Enforcement** (`COMPLIANCE_ENFORCED=true`): placing an offer requires an
  approved vehicle **and** approved assigned driver.

| Method & path | Purpose |
|---------------|---------|
| `POST /api/admin/agencies` · `POST /api/admin/region-rules` | configure approved issuers & per-region rules |
| `POST /api/drivers` · `POST /api/vehicles` · `POST /api/service-centers` | register entities |
| `POST /api/compliance/documents` | attach a document (auto-hashed for dedup) |
| `POST /api/vehicles/{id}/inspection` | record a physical/mechanical inspection |
| `POST /api/{drivers|vehicles|service-centers}/{id}/submit` | run the auto check |
| `GET /api/compliance/decisions` | decisions with reasons, signals, risk score |
| `POST /api/admin/compliance/decisions/{id}/override` | limited human intervention |

## Load modes, scope, matching & consolidation

Loads carry a **mode** and an auto-derived **scope**:

- `ftl` (full truckload) — `GET /api/loads/{ref}/match-vehicles` ranks eligible
  vehicles by type/tonnage (smallest sufficient capacity first), honouring
  special-handling features (refrigeration, hazmat, livestock) and, when enforced,
  compliance approval.
- `ltl` (less-than-truckload) — candidates for consolidation.
- `consolidation` — `GET /api/consolidations/suggest` groups open LTL loads by
  origin/destination corridor (optionally capped by weight); `POST
  /api/consolidations` aggregates chosen loads into one group.
- **Scope** (`local` / `intra_region` / `inter_region`) is derived from the
  origin/destination regions and distance at load creation.
- **Optional cargo insurance** is selectable by the load owner at placement
  (`insurance_opted`, `insurance_level`, `insurance_value`).

## prisaMove platform enhancements

| Capability | Endpoints | Notes |
|------------|-----------|-------|
| **Reputation & scoring** | `POST /api/ratings`, `GET /api/carriers/{id}/reputation` | avg rating + on-time / cancellation performance → composite trust score |
| **Telematics ingestion** | `POST /api/telematics/heartbeat`, `/event`, `GET /api/vehicles/{id}/telematics` | device pings drive the live track; `tamper` auto-suspends the vehicle; stale heartbeat → offline |
| **Escrow + ePOD + settlement** | award opens escrow; `POST /api/shipments/{id}/epod`, `POST /api/escrows/{id}/release` | release requires a valid ePOD and settles **net of statutory tax/withholding** (tax engine); gateway is a pluggable adapter (`manual` default) |
| **Insurance marketplace** | `POST /api/admin/insurance-rates`, `/api/insurance/quote|bind|claims` | premiums from operator/insurer-configured rates; claims workflow tied to a shipment |
| **Dynamic pricing & demand** | `GET /api/regions/{c}/surge`, `/api/lanes/{o}/{d}/benchmark`, `/api/loads/{ref}/backhaul`; carbon in estimates | surge from open-load/vehicle balance; backhaul = return-trip matching; CO₂e from standard emission factors |
| **Fleet & HOS** | `GET /api/vehicles/{id}/service-status`, `POST /api/drivers/{id}/duty`, `GET /api/drivers/{id}/hours` | inspection service-due; hours-of-service from duty logs |
| **Trust & safety** | `GET /api/ops/review-queue`, `/api/trust/clusters` | review queue (risk-ranked); self-inspection rings & shared-document clusters |
| **Resilience** | `Idempotency-Key` header; `X-Tenant-ID`; admin audit log; Redis-backed event bus (`REDIS_URL`) | exactly-once mutations; auditable admin actions; SSE across workers |
| **Channels & PWA** | `POST /api/notifications/send` (sms/whatsapp/push); `frontend/` PWA (manifest + service worker) | provider webhooks (console fallback); offline-capable app shell |
| **KYC / verification** | `POST /api/compliance/documents/{id}/verify`, `POST /api/compliance/screen` (auto on document submit) | OCR/issuer verification + sanctions screening via a provider; **manual provider verifies nothing** — no fabricated verification |
| **Multi-tenant isolation** | `X-Tenant-ID` header + `TENANT_ISOLATION=true` | **central** SQLAlchemy guard auto-stamps new rows and filters **all** ORM reads for every tenant model (cross-tenant access → 404) |
| **Service-station onboarding** | `POST /api/service-centers/{id}/submit`, `/risk`, `/reputation`; `POST /api/admin/service-centers/{id}/status` | regulatory compliance + risk assessment + reputation, lifecycle `pending→under_review→approved/rejected/suspended`; self-inspection rings auto-route to review |
| **Party onboarding (people & orgs)** | `POST /api/parties` + `/{id}/submit`, `/risk`; `POST /api/admin/role-requirements`, `POST /api/admin/parties/{id}/status`; drivers via `/api/drivers/{id}/submit` + `/api/admin/drivers/{id}/status` | one engine for **drivers, logistics operators, fleet managers, inspection agents, MSPs**: admin-configured per-region/role docs + experience + sanctions screening; risk + lifecycle + admin transitions |

`KYC_ENDPOINT`/`SANCTIONS_ENDPOINT` plug real verification providers; without them
documents stay unverified (operator/manual review). With `REDIS_URL` set, the SSE
event bus uses Redis pub/sub so live tracking streams fan across gunicorn workers.

**Tenant isolation is enforced centrally**: `tenants.install_tenant_guard` registers
a `before_flush` hook (stamp `tenant_id` on every new row) and a `do_orm_execute`
hook (apply a tenant predicate to every ORM SELECT, including joins, counts and
lazy loads), so coverage is uniform across all endpoints rather than per-handler.

**Unified party onboarding** (`onboarding.py`) applies one deterministic state
machine to **drivers, logistics operators, fleet managers, platform inspection
agents and MSPs**. Requirements are admin-managed per `(region, role)`
(`RoleRequirement`; drivers reuse `RegionComplianceRule`): required documents from
approved agencies, minimum experience, and optional sanctions/PEP **screening**
via the KYC provider (a screening requirement with no real provider → review, not
auto-approve). Risk signals (re-used documents, screening hits, inspection-agent
conflict of interest, poor reputation) route clean parties to review; admins
transition status (approve/suspend/reinstate/reject) with auditable reasons.

**Service-station onboarding** is a state machine: a centre is registered, submits
regulatory documents (per-region `required_service_center_docs` from approved
agencies), and `submit` runs compliance + a risk assessment (self-inspection ring,
re-used documents, anomalous 100% pass rate, poor reputation) to land it in
`approved`, `rejected` or `under_review`. Admins transition status
(approve/suspend/reinstate/reject) with an auditable reason, and reputation blends
ratings with inspection pass-rate.

These build on the same principles: deterministic logic over real data, with external
providers (payment gateway, insurers, SMS/WhatsApp) behind adapters that do nothing
until configured — no fabricated transactions, rates or deliveries.

## prisaMove live shipment tracking

Awarding an offer (`POST /api/offers/{id}/accept`) opens a trackable **shipment**.
Drivers/operators report real GPS positions; ETA is computed from the reported
location to the destination (remaining road distance ÷ average speed + accumulated
delay) — real inputs, not a simulation.

| Method & path | Purpose |
|---------------|---------|
| `GET /api/shipments` · `GET /api/shipments/{id}` | list / fetch shipments |
| `GET /api/loads/{ref}/shipment` | shipment for a load |
| `POST /api/shipments/{id}/location` | driver/operator GPS ping (updates ETA) |
| `GET /api/shipments/{id}/track` | breadcrumb trail (for the map) |
| `POST /api/shipments/{id}/status` | picked_up / en_route / arrived / delivered |
| `POST /api/shipments/{id}/delay` | report a delay with a reason (recomputes ETA) |
| `POST /api/shipments/{id}/reroute` | reroute (calls prisaTravel for a hazard-avoiding path) |
| `GET /api/shipments/{id}/events` | shipment timeline |
| `GET /api/shipments/{id}/stream` | **Server-Sent Events** live push stream |
| `GET /api/tracking/reasons?category=` | **pre-seeded** delay/reroute reasons |
| `POST /api/admin/tracking-reasons` | add a custom reason (`X-Admin-Key`) |

**Real-time push.** Every shipment event is published to an in-process event bus
and streamed over **Server-Sent Events** (`GET /api/shipments/{id}/stream`); the
map (`frontend/tracking.html`) updates from the SSE stream (with a slow poll as a
fallback). The bus is process-local — back it with Redis pub/sub to fan across
multiple gunicorn workers/replicas.

**Hazard-aware reroute.** `POST /api/shipments/{id}/reroute` with a `reason_code`
and optional `hazard` `{lat,lon,radius_km}` calls **prisaTravel (TRAAS)**: it
registers the hazard, requests a hazard-avoiding route from the current position,
and feeds the returned distance back into the ETA as a `route_factor` that carries
the detour penalty forward (set `TRAAS_URL`). Without TRAAS, an operator-supplied
`new_distance_km` is used.

Delay/reroute/ETA/status changes are also dispatched to a notifier (console log by
default, or a webhook via `TRACKING_WEBHOOK_URL`). The map view has a
driver/operator control panel. Reason selection is pre-seeded (traffic, accident,
breakdown, weather, flooding, checkpoint, road closure, border delay, …) and
admin-extensible.

```bash
# After accepting an offer you get a shipment_id; drive it and watch ETA move
curl -s localhost:5003/api/shipments/$SID/location -H 'content-type: application/json' \
  -d '{"lat":11.5,"lon":12.5,"speed_kmh":60}'
curl -s localhost:5003/api/shipments/$SID/delay -H 'content-type: application/json' \
  -d '{"delay_minutes":90,"reason_code":"security_checkpoint"}'
```

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
| `ADMIN_API_KEYS` | _empty → admin disabled_ | lgaas (tax/compliance admin) |
| `COMPLIANCE_ENFORCED` | `false` | lgaas (offers require approved vehicle+driver) |
| `TRAAS_URL` | _unset_ | lgaas (reroute avoidance) |
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
                 geo, weather, db, regions, i18n, localization, currency, tax,
                 holidays, holiday_provider, eventbus, channels
  wfaas/         models, service, notifications, monitoring, ai, app, wsgi, Dockerfile
  traas/         models, routing, service, app, wsgi, Dockerfile
  marketplace/   models, pricing, negotiation, service, tax_service, calendar_service,
                 tracking, routing_client, compliance, compliance_service, loads_planning,
                 reputation, telematics, payments, insurance, demand, fleet, trust,
                 resilience, kyc, tenants, service_centers, onboarding, app, wsgi, Dockerfile
migrations/      Alembic envs for wfaas / traas / lgaas
frontend/        index.html (GIS dashboard) + marketplace.html (prisaMove)
tests/           pytest suite
manage.py        ops CLI (init-db, seed, scan)
```
