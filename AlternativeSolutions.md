# Alternative Solutions

> This document is a companion to `README.md` and `AssignmentUnderstanding.md`.
> The shipped implementation uses **Django + DRF + Redis** with a token-bucket
> rate limiter, a ZSET-based concurrency semaphore, and per-tenant Redis key
> namespacing. This document asks the honest question: *what else could we have
> done, and when would those alternatives be the right choice?*
>
> The goal is not to argue the shipped design is wrong. It is to make the design
> space legible — so a reader can see why Redis was picked, what the trade-offs
> were, and what shape the solution would take if one of the constraints changed.

---

## 1. What the problem actually is

Strip away the framing and the assignment is really three problems stacked:

| # | Problem                                    | What it demands from the infra                                                                                      |
| - | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| 1 | **Admission control**                      | A per-tenant arrival-rate limit and a per-tenant in-flight concurrency cap, enforced before any expensive work runs |
| 2 | **Usage-event ingestion**                  | Idempotent, durable append of millions of events/day, partitioned by tenant                                         |
| 3 | **Rating / invoice generation**            | Read a window of a tenant's events, multiply by price, persist a draft, later flip it to issued                     |

Any viable solution has to answer all three. The shipped solution answers them
with one tool (Redis). Every alternative below picks a *different* mix of tools
and accepts a *different* set of trade-offs.

---

## 2. Why Redis was picked (and when it stops being the right pick)

Redis is the right choice when all of these hold:

- Latency budget for admission checks is **single-digit milliseconds** (Redis
  easily does this; a relational DB rarely does reliably).
- Atomic multi-step operations are needed (token bucket, semaphore slot release
  on TTL expiry) — Redis gives you this via Lua scripts, no distributed lock.
- Write volume is high but the working set fits in memory.
- You are comfortable losing the last few seconds of data on a node failure
  (admission state is ephemeral; event data can be treated separately).

Redis becomes the **wrong** choice when:

- You need strong durability guarantees on every write (Redis AOF + fsync is
  possible but expensive; streams are not a message-broker replacement).
- You need analytical queries over historical events (Redis Streams are not
  queryable; you'd be reinventing a database).
- The team operating the service can't run a Redis cluster well (the Ops cost
  of a misconfigured Redis is higher than the Ops cost of Postgres in most orgs).

Everything below is a response to one of those "wrong choice" conditions.

---

## 3. Alternative architectures

### 3.1 Postgres-only (no Redis at all)

**Shape.** Keep Django and DRF; drop Redis entirely. Use Postgres for
everything.

- **Admission control:** a small `admission_counters` table keyed on
  `(tenant_id, bucket_window)` updated via `INSERT ... ON CONFLICT DO UPDATE`.
  Concurrency tracking via an `in_flight_slots` table with `LOCK TABLE ... IN
  EXCLUSIVE MODE` or advisory locks (`pg_advisory_xact_lock`).
- **Event ingestion:** insert into a partitioned `events` table(UNLOGGED TABLES as the writes are much faster on UNLOGGED Tables as compared to Default Tables in PostgreSQL), one partition
  per tenant or one per day-per-tenant. Idempotency enforced by a unique index
  on `(tenant_id, idempotency_key)`.
- **Rating:** normal SQL — `SELECT count(*) FROM events WHERE tenant_id = ?
  AND timestamp BETWEEN ? AND ?`.

**Why this is attractive.**
- **One system to run.** No Redis to tune, monitor, or explain to on-call.
- **Transactional ingest.** Idempotency marker and event row go into the same
  transaction — no two-round-trip gap like the current Redis implementation has.
- **Queryable forever.** Rating windows, ad-hoc audits, billing reconciliation
  — all trivial SQL.

**Why it was not chosen.**
- Admission-path latency. Every HTTP request would take a Postgres round-trip
  on the hot path; p99 under load is fragile when row locks contend. Redis'
  sub-millisecond atomic ops are a much better fit for a gate that runs on
  *every* request.
- Advisory locks don't play well with connection pools and short-lived requests
  — a crashed request leaves the slot held until the TX ends.
- Event write throughput: inserting millions of rows/day into a single table
  requires careful partitioning; Redis Streams handle this shape natively.

**When it would be the right pick.** Small-to-medium scale (thousands of events
per tenant per day, tens of tenants), small Ops team, strong need for
auditability. Most B2B SaaS billing systems at sub-enterprise scale would be
fine here.

---

### 3.2 Kafka (or Kinesis / Pub-Sub) for ingest; Postgres for rating; Redis *only* for admission

**Shape.** A three-tier solution that plays to each system's strength.

- **Admission control:** Redis, exactly as shipped (or replace with an
  API-gateway feature like Envoy's rate-limit service / AWS WAF / Kong).
- **Event ingestion:** write each event to a Kafka topic partitioned by
  `tenant_id`. Idempotency via the producer's transactional / idempotent
  producer feature (built-in since Kafka 0.11).
- **Rating:** a consumer service reads from Kafka, aggregates into Postgres
  (or ClickHouse / BigQuery for heavy analytical workloads). Invoice generation
  reads from Postgres.

**Why this is attractive.**
- **Back-pressure is real.** Kafka absorbs spikes; the rating consumer drains
  at its own pace. The current Redis-Streams approach technically does this,
  but Kafka is purpose-built for it.
- **Replay.** Want to re-rate three months because pricing changed? Rewind the
  Kafka offset. In the current design you'd have to keep Redis Streams around
  forever (expensive) or restore from a backup.
- **Fan-out.** A second consumer (alerting, anomaly detection, data science)
  can read the same stream without affecting billing.
- **Durability.** Kafka replicates; losing a broker doesn't lose events.

**Why it was not chosen.**
- Complexity far beyond the assignment's scope. Running Kafka requires ZooKeeper
  (or KRaft), schema registry, consumer-group management, DLQs — an order of
  magnitude more Ops.
- The assignment's incident was about *admission*, not *event durability*.
  Kafka fixes a problem we don't have yet.

**When it would be the right pick.** When event volume crosses roughly 10k/sec
sustained, or when the business needs multiple consumers of the same event
stream, or when replay / reprocessing is a recurring requirement.

---

### 3.3 API Gateway for admission; App does the rest

**Shape.** Take admission control out of the application entirely.

- **Admission control:** AWS API Gateway usage plans / Envoy rate-limit
  service / Kong / Cloudflare. Per-API-key tiers, built-in.
- **Event ingestion + rating:** whatever the app prefers (the current Redis
  design, or Postgres-only).

**Why this is attractive.**
- **Zero application code for the gate.** The incident root cause (slow
  admission checks held Redis connections too long) literally cannot happen
  because the gate doesn't talk to Redis — it's in the edge.
- **Consistent behaviour across services.** If the org has multiple services,
  one gateway rate-limiter covers all of them.
- **Free DDoS protection at the edge.** Bonus.

**Why it was not chosen.**
- The assignment is explicitly a coding exercise. "Put it behind Cloudflare" is
  a correct answer in production and a non-answer to the assignment.
- Gateway rate-limits typically don't have a rich notion of "concurrency cap"
  (they think in requests-per-second). Concurrency was the pointed fix for
  acme-corp; the gateway alone wouldn't have caught the backfill symptom.

**When it would be the right pick.** Any production deployment. Seriously —
arrival-rate limiting at the edge is cheaper, simpler, and more robust than
doing it in the app. The app-side concurrency cap can still live in the app,
or be replaced by per-pod concurrency limits in the orchestrator (Kubernetes
`maxConcurrentRequests`, Istio outlier detection, etc.).

---

### 3.3 Per-tenant workers / queues (the "bulkhead" pattern)

**Shape.** Give each tenant — or each tier — its own worker pool, its own
queue, its own budget of Redis connections. Noisy tenants drown in their own
pool; quiet tenants are physically isolated.

- **Admission control:** implicit in the sizing — if a tenant's pool is full,
  their requests block or 503 there, and no other tenant sees the pressure.
- **Event ingestion:** per-tenant queue (SQS / Celery with separate queues /
  Temporal task queues).
- **Rating:** per-tenant or per-tier worker pool.

**Why this is attractive.**
- **Strongest possible isolation.** It's literally a separate process. No
  amount of Redis pool exhaustion in tenant A's workers can hurt tenant B's
  workers.
- **Conceptually simple.** No shared atomic counters, no Lua scripts.

**Why it was not chosen.**
- Multi-tenant SaaS operates on the assumption that *tenants share
  infrastructure* — that's what makes it profitable. Giving every tenant their
  own worker is closer to single-tenant hosting and scales linearly in cost.
- Only the biggest enterprise tenants would justify dedicated pools; the
  long tail still needs shared admission control anyway.

**When it would be the right pick.** Regulated industries (healthcare,
finance) where cross-tenant blast radius is a compliance issue, or for a
small number of very-large enterprise customers on a premium tier.

---

---

## 5. Summary

- Redis is a defensible choice and maps well to the assignment's constraints.
- Postgres-only is the most honest alternative for sub-enterprise scale.
- Kafka + Postgres is where you end up when event durability and replay matter.
- Per-tenant workers are the nuclear option for isolation.


The shipped solution is a good fit for **this** assignment. A different
combination of the above would be the better fit for a different version of
the same problem.
