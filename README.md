# Multi-Tenant Billing — Noisy-Neighbor Admission Control

A Django + DRF + Redis service that demonstrates the noisy-neighbor
incident from the Zenskar assignment, and the two-gate admission
control that fixes it.

- Layered (ports-and-adapters / DDD): `interfaces → application → domain / infrastructure`
- Real Redis is used throughout — no mocks, in production code or tests
- Two admission gates: per-tenant token bucket (arrival rate) + per-tenant concurrency semaphore (in-flight count)
- Full pytest harness (unit + live-server integration) reproducing the incident and proving the fix
- Standalone load runner for visual demos

---

## 1. The incident

The platform runs on shared infrastructure. Redis is single-threaded
and its command queue is shared across all tenants. When
`acme-corp`'s backfill job fired thousands of events per second into
the ingest path, three things happened in sequence:

1. Redis's command queue deepened. Each individual op still took
   microseconds, but the **wait time** before Redis picked it up
   ballooned.
2. Because every app-side Redis call now took tens–hundreds of
   milliseconds instead of <1 ms, each check-out from the shared
   connection pool held a socket **100× longer**.
3. The pool exhausted. `globex` and `initech` — who were sending
   normal, light traffic — got `connection pool exhausted` errors on
   endpoints that had nothing to do with acme-corp.

The logs show the classic monotonic slowdown signature:

```
globex /billing/calculate  234ms → 891ms → 2103ms → 7841ms 504
                                                     ↑
                                              connection pool exhausted
```

A full incident walkthrough (including why "just add more Redis"
doesn't fix this) is preserved in the commit history and the old
notes section below.

## 2. Why one gate is not enough

The fix uses **two independent admission gates**, because the incident
has two independent causes.

| Gate | What it counts | What it stops |
|---|---|---|
| Token bucket (`RateLimitMiddleware`) | New requests per second, per tenant | A tenant sending *too many starts* per second |
| Concurrency semaphore (`ConcurrencyMiddleware`) | In-flight requests per tenant on heavy endpoints | A tenant whose requests are *too slow*, piling up in-flight and holding connections |

The critical insight: a tenant firing `5 calls/sec` where each call
takes `10s` is trivially under any sane rate limit, and yet at steady
state they have **50 concurrent requests** pinning 50 worker threads
and 50 Redis connections. That's the acme-corp incident exactly.
Without the second gate, the token bucket is blind to it.

```
[HTTP request]
      │
      ▼
┌──────────────────────┐     429 rate_limited          ┌───────────┐
│ RateLimitMiddleware  │──────────────────────────────▶│ client    │
└─────────┬────────────┘                                └───────────┘
          │ allowed
          ▼
┌──────────────────────┐     429 concurrency_limit     ┌───────────┐
│ ConcurrencyMiddleware│──────────────────────────────▶│ client    │
└─────────┬────────────┘         (heavy endpoints only)
          │ slot acquired
          ▼
┌──────────────────────┐
│       view           │
│   application use    │
│        case          │
│      Redis I/O       │
└──────────────────────┘
          │
          ▼
    (slot released in finally)
```

Both gates fail open: if the gate itself errors (Redis blip, Lua
bug), the request is allowed through. A broken limiter must never
become a platform outage.

## 3. Architecture

```
billing/
├── interfaces/api/incoming/       # HTTP adapter
│   ├── urls.py
│   ├── views.py                   # IngestView, BillingCalculateView, InvoiceGenerateView
│   ├── serializers.py             # DRF serializers (one per endpoint)
│   ├── middleware.py              # RateLimitMiddleware, ConcurrencyMiddleware
│   └── tenant.py                  # resolve_tenant helper
├── application/                   # use cases — pure business logic
│   ├── ingest_event.py
│   ├── rate_limiter.py            # cost table, plan lookup, delegates to bucket
│   ├── billing_calculator.py
│   └── invoice_generator.py
├── domain/
│   └── tenant_plan.py             # TierLimits, PLANS catalog, plan_for()
└── infrastructure/                # Redis-specific mechanics
    ├── redis.py                   # singleton client factory
    ├── token_bucket.py            # atomic Lua bucket
    ├── concurrency_semaphore.py   # atomic Lua ZSET semaphore
    └── event_repository.py        # claim-first idempotent writes
```

Dependency direction is strict: `interfaces → application → domain / infrastructure`.
The application layer takes plain Python values and returns plain
Python values — it never sees a DRF `Request`, never renders a
`Response`, and never constructs HTTP status codes.

### Tier catalog

Defined in `billing/domain/tenant_plan.py`. All three fields on
`TierLimits` matter:

| Plan | rps | burst | max_concurrency | Used for |
|---|---|---|---|---|
| enterprise | 500 | 1000 | 20 | globex |
| standard | 100 | 200 | 10 | initech |
| small | 20 | 50 | 3 | piedmont |
| backfill | 50 | 100 | 5 | acme-corp |
| default | 100 | 200 | 5 | unknown tenants |

## 4. Running locally

### Prerequisites

- Python 3.12
- Redis running locally on `localhost:6379`

Start Redis (macOS via Homebrew):

```bash
brew services start redis
redis-cli ping  # expect "PONG"
```

### Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run the server

```bash
python manage.py runserver 8000
```

The server listens on `http://127.0.0.1:8000`.

### Quick smoke test

```bash
# ingest an event for globex
curl -s -X POST http://127.0.0.1:8000/api/v1/events/ingest \
  -H "Content-Type: application/json" -H "X-Tenant-ID: globex" \
  -d '{
    "event_name":"api_call",
    "customer_id":"cust-1",
    "timestamp":"2026-04-01T12:00:00Z",
    "idempotency_key":"smoke-1",
    "value":1
  }'

# calculate a draft
curl -s -X POST http://127.0.0.1:8000/api/v1/billing/calculate \
  -H "Content-Type: application/json" -H "X-Tenant-ID: globex" \
  -d '{
    "customer_id":"cust-1",
    "period_start":"2026-04-01T00:00:00Z",
    "period_end":"2026-05-01T00:00:00Z"
  }'

# issue it
curl -s -X POST http://127.0.0.1:8000/api/v1/invoices/generate \
  -H "Content-Type: application/json" -H "X-Tenant-ID: globex" \
  -d '{"invoice_id":"inv_<paste from calculate>"}'
```

## 5. Tests

The suite is split into `tests/unit/` and `tests/integration/`.
**All tests run against real Redis** (db=15, isolated from dev by
a tripwire assertion in `tests/conftest.py`).

Run everything:

```bash
source .venv/bin/activate
pytest
```

Run just the incident-reproduction integration test:

```bash
pytest tests/integration/test_incident_reproduction.py -v
```

The incident test has three scenarios:

1. **Headline** — with both gates on, a piedmont calculate-flood
   produces many `concurrency_limit` 429s *and* a globex ingest load
   running in parallel is 100% successful. Tenant isolation
   demonstrated at the request level.

2. **Counterfactual** — with `ConcurrencyMiddleware` removed via
   `override_settings`, piedmont's flood runs far past its cap=3. This
   is the pre-fix incident shape, turned on and off in one file.

3. **Ingest-path isolation** — rate limiter alone successfully caps
   the small tenant while the enterprise tenant passes through
   untouched at the same volume.

### Test isolation model

- `tests/conftest.py` forces `REDIS_URL=redis://localhost:6379/15` at
  import time, *before* Django settings load.
- `_flush_redis_between_tests` is an autouse fixture: FLUSHDB before
  and after every test, plus `get_redis_client.cache_clear()`.
- A tripwire assertion refuses to run if `REDIS_URL` is not db=15 —
  you cannot accidentally wipe dev state.

## 6. Visual demo: `scripts/simulate_incident.py`

A stdlib-only load runner that fires noisy + quiet workloads in
parallel against a running server and prints a per-tenant outcome
summary.

```bash
# in one terminal
python manage.py runserver 8000

# in another terminal
python scripts/simulate_incident.py
```

Typical output **with the fix in place** (both middlewares enabled):

```
== Simulating noisy-neighbor against http://127.0.0.1:8000
   noisy: 'acme-corp' → /api/v1/billing/calculate × 60 req, 15 workers
   quiet: 'globex' → /api/v1/events/ingest × 30 req, 5 workers

Results
-------
  NOISY   tenant='acme-corp'    endpoint=/api/v1/billing/calculate
          total=60  success=~5  rate_limited=0  concurrency_limit=~55  error=0
  QUIET   tenant='globex'       endpoint=/api/v1/events/ingest
          total=30  success=30  rate_limited=0  concurrency_limit=0    error=0

Tenant isolation holding: the quiet tenant was unaffected.
```

To see the pre-fix behaviour, comment out `ConcurrencyMiddleware` in
`billing_platform/settings.py` and re-run: the noisy tenant admits
many more concurrent calls, and under heavy enough flood the quiet
tenant starts seeing slowdowns.

Configurable flags:

```bash
python scripts/simulate_incident.py \
    --noisy piedmont --quiet initech \
    --noisy-requests 120 --noisy-concurrency 30 \
    --quiet-requests 60  --quiet-concurrency 10
```

## 7. Intentional simplifications

These are conscious scope decisions for the assignment — each is
called out in the relevant code:

| Simplification | Where | Rationale |
|---|---|---|
| `XLEN` instead of period-filtered `XRANGE` | `billing_calculator.py` | We care about the *shape* (per-tenant Redis call holding a connection), not the aggregation logic. |
| Flat `price_per_event` | `billing_calculator.py` | Tiered rates and discounts don't change the concurrency story. |
| Issued invoices live in Redis, not a durable store | `invoice_generator.py` | Postgres/S3 ledger is orthogonal to the admission problem. |
| Idempotency claim and stream append are two separate calls | `event_repository.py` | Crash-window is small; a Lua-atomic version is a one-commit upgrade noted inline. |
| Tier catalog is Python constants | `tenant_plan.py` | Real system would read from a config service; this keeps the policy visible in review. |
| `X-Tenant-ID` is trusted | `tenant.py` | Real auth (JWT → tenant claim) belongs upstream; out of scope. |
| Draft TTL fixed at 1 hour | `billing_calculator.py` | Tunable; per-tenant override is straightforward. |

## 8. Future work

- Lua-atomic `(SET NX) + XADD` in the event repository.
- Dynamic tier config via a Redis-backed catalog so ops can adjust
  limits without a deploy.
- Postgres ledger for issued invoices (durable store).
- Structured emission of admission metrics (Prometheus / OTEL) so
  ops dashboards show noisy-neighbor signals before they page.
- Per-endpoint concurrency caps inside a tier (today the cap is
  tier-wide across all heavy endpoints).

---

## Appendix A — Original incident analysis notes

The following was written while working through the problem and is
kept because it captures the reasoning that led to the two-gate
design. Shorter summary is in section 2.

### Key explanations of what I understood from the Redis setup and how it's operating

1. The platform runs on shared ECS clusters. Redis is shared across
   all tenants. That means one Redis instance (or one Redis Cluster)
   serves acme, globex, initech, piedmont — all of them. There is no
   per-tenant Redis. Every XADD, every SET, every INCR from every
   tenant hits the same Redis process.

2. Redis processes commands from all clients through one FIFO-ish
   queue. Acme's commands and globex's commands wait in the same line.

3. Redis operation's latency has TWO parts:

   ```
   total latency for one op = wait_time + service_time
                               ↑            ↑
                               time spent   time Redis actually
                               in the       takes to execute
                               queue        your command
                               waiting
                               your turn
   ```

   **Service time** = how long Redis takes to actually run your
   command once it starts. For a SET or XADD, this is
   microseconds — typically 50–200 µs. It is essentially constant
   regardless of load.

   **Wait time** = how long your command sits in the queue before
   Redis picks it up. Under light load this is ~0. Under heavy load
   it grows — can reach seconds.

4. **Application-side Redis connection pool.** Each Django/ECS worker
   process holds a client-side pool of Redis connections (typically
   20–50 sockets). When the view code does `r.set(...)`:

   1. Checks a connection out of the pool
   2. Writes the command to the socket
   3. Waits for the response
   4. Returns the connection to the pool

   A connection is "held" for the duration of that wait — usually
   <1 ms when Redis is idle. Under acme's flood, the pool still has
   20 sockets but each is held much longer (point 5). When all 20
   are held, the 21st request has nothing to check out.
   `connection pool exhausted` is that case.

5. **How queue depth poisons the connection pool.** Redis is still
   fast per command, but the queue is now deep. When globex's
   `/billing/calculate` needs to do 5 Redis operations, each of
   those 5 has to wait behind thousands of acme's ops. `5 × 1 ms`
   becomes `5 × 200 ms = 1 s` — and then blows past the 5-second
   client timeout.

   The cascade:

   ```
   Acme's flood
     → Redis server command queue grows
     → each Redis op takes 100×+ longer
     → application holds pool connections 100×+ longer
     → pool exhausts
     → new requests fail before they even reach Redis
     → 503 service unavailable
   ```

   That's why globex and initech, who weren't even sending much
   traffic, got 503s. They were never the problem — they were just
   waiting in the same line.

### Why acme's individual requests stayed fast

Acme's individual operations (one XADD and one SET per request) are
cheap — a few Redis commands each. Globex's `/billing/calculate`
needs ~20 Redis ops; each of those sits in the same queue acme is
filling. Damage is not proportional to fault.

### Why the fix isn't "just add more Redis"

- **Vertical scale** — bigger box doesn't help because command
  execution is single-threaded. The bottleneck core is unchanged.
- **Redis Cluster (sharding)** — helps if you shard by tenant. But a
  single hot tenant (acme) still pins one shard.
- **Bigger connection pool** — just delays the cliff. 200 connections
  means Redis is now flooded with 10× the concurrent commands,
  making the queue worse.
- **Async everything** — kicks the problem one layer down but the
  underlying resource contention doesn't disappear.

**What actually fixes it:** admission control — stop acme at the
front door before they can touch Redis, using a budget they're
allowed to consume. That's the rate limiter + per-tenant concurrency
cap built in this repo.
