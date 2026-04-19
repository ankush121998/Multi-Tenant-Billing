# Tests — how to run them, and what they prove

This folder holds every test for the admission-control fix. The
structure is:

```
tests/
  conftest.py                  # shared fixtures (Redis flush, live_server)
  unit/                        # one module per component under test
    test_token_bucket.py
    test_concurrency_semaphore.py
    test_event_repository.py
    test_tenant_plan.py
    test_billing_calculator.py
    test_invoice_generator.py
  integration/                 # end-to-end through Django + Redis
    test_admission_flow.py
    test_incident_reproduction.py
```

---

## 1. Prerequisites

1. A virtualenv at `.venv/` with requirements installed:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. A local Redis reachable at `localhost:6379`. The test suite uses
   **db=15** exclusively and flushes it before and after every test —
   a tripwire assertion in `tests/conftest.py` refuses to run if the
   env-var doesn't point at db=15, so there's no risk of wiping your
   dev data.
3. Nothing else. No Django migrations, no fixtures, no seed data.

### Starting Redis locally

```
brew install redis           # macOS
brew services start redis
# or:
redis-server                 # runs in the foreground
```

---

## 2. Running the tests

All commands assume the project root as CWD.

| Goal | Command |
|---|---|
| Run everything (48 tests, ~4 s) | `.venv/bin/pytest` |
| Run only unit tests | `.venv/bin/pytest tests/unit` |
| Run only integration tests | `.venv/bin/pytest tests/integration` |
| Run one file | `.venv/bin/pytest tests/unit/test_token_bucket.py` |
| Run one test | `.venv/bin/pytest tests/unit/test_token_bucket.py::test_concurrent_consumers_cannot_overdraw_capacity` |
| See captured stdout (narrative) for each test | already configured — `pytest.ini` has `-v -rA --capture=tee-sys` |
| Show only short summary on failure | `.venv/bin/pytest -ra` |
| Stop at first failure | `.venv/bin/pytest -x` |
| Show the slowest tests | `.venv/bin/pytest --durations=10` |

### What you'll see

Every test prints a short narrative of the scenario it's proving and
the numbers it observed. Output shape is:

```
tests/unit/test_token_bucket.py::test_concurrent_consumers_cannot_overdraw_capacity
----------------------------- Captured stdout call -----------------------------
[token_bucket] Scenario: fire 100 threads at a capacity=20 bucket with
refill_rate=0. Without Lua atomicity a race could admit more than 20. We
assert admitted == 20 exactly.
  observed: threads=100, admitted=20, capacity=20
  PASS: atomic Lua serialises all 100 concurrent consumers; no overdraw.
PASSED
```

You get the same narrative whether the test passes or fails; failures
get the full traceback on top.

---

## 3. The tests, one by one

Each test file has a module-level docstring explaining scope, rationale,
and run-instructions. Each test function has its own docstring. What
follows is a reader's guide at a higher level.

Legend:
- **[fix-correctness]** — verifies the mitigation works. Does not
  reproduce the incident. Would not exist on the buggy system.
- **[api-shape]** — verifies HTTP contract (status codes, headers).
  Would pass on the buggy system.
- **[reproduction]** — **would fail on the buggy system.** These are
  the tests the assignment rubric demands.
- **[demonstration]** — simulates the bug (e.g. by disabling the fix).
  Illustrates the bug shape but is not itself a reproduction.

### 3.1 `tests/unit/test_token_bucket.py` (6 tests — all `[fix-correctness]`)

The atomic token-bucket rate limiter.

| Test | What it proves | Why it matters |
|---|---|---|
| `test_first_consume_is_allowed_on_a_fresh_bucket` | A brand-new bucket initialises at full capacity and decrements by `cost` on first consume. | If the bucket were lazily empty, a tenant's first request of the day would be rejected. |
| `test_consumes_until_empty_then_rejects` | After `capacity` admissions the next consume is rejected with a `retry_after > 0`. | Clients need a backoff hint; without it they retry-storm and make the incident worse. |
| `test_refills_at_the_configured_rate` | After draining, sleeping 0.7 s with `refill_rate=5/s` returns at least 2 admissions out of 4 attempts. | Refill must actually happen — a bucket that only drains is a denial-of-service. |
| `test_capacity_is_an_upper_bound_on_tokens` | Idle for 0.2 s with `refill_rate=100` and `capacity=3` — admits exactly 3, not 20. | Otherwise a long-idle tenant could burst past their plan. |
| `test_concurrent_consumers_cannot_overdraw_capacity` | 100 threads racing at one `capacity=20` bucket admit *exactly* 20. | **The whole reason the check is a Lua script.** Without atomicity a race would admit more than capacity. |
| `test_cost_weighting_subtracts_more_than_one` | `cost=10` per call drains a `capacity=10` bucket in one call. | Heavy endpoints like `/billing/calculate` cost more than ingests — a flat "one token per request" policy would let heavy calls monopolise a shared bucket. |

### 3.2 `tests/unit/test_concurrency_semaphore.py` (6 tests — all `[fix-correctness]`)

The per-tenant concurrency semaphore (the second admission gate — the
fix that really solved the incident).

| Test | What it proves | Why it matters |
|---|---|---|
| `test_acquire_admits_up_to_capacity` | First `capacity` acquires succeed; the next is rejected. | Cap is enforced. |
| `test_release_frees_a_slot` | After releasing a holder, a fresh acquire succeeds. | Without release, every tenant would lock up after `max_concurrency` long-lived calls forever. |
| `test_release_of_unknown_slot_is_harmless` | Releasing a slot_id that was never acquired neither raises nor corrupts the counter. | Tolerance for network-retry / middleware-exception paths where release might double-fire or fire for a missing acquire. |
| `test_stale_slots_self_evict` | A slot written with a score older than `SLOT_TTL_SECONDS` is ZREMRANGEBYSCORE'd out on next acquire. | **If a worker crashes mid-request, its slot must not occupy the quota forever** — otherwise the tenant locks themselves out permanently. |
| `test_concurrent_acquires_never_exceed_capacity` | 50 threads racing at `capacity=5` admit *exactly* 5. | Same atomicity argument as the token bucket; without it the whole gate is a lie under load. |
| `test_different_keys_are_independent` | Tenant-a filling its cap does not reduce tenant-b's admission budget. | **The isolation property the incident exposed.** Without it, one tenant's load leaks into another's capacity. |

### 3.3 `tests/unit/test_event_repository.py` (6 tests — all `[fix-correctness]`)

The event repository's idempotency + per-tenant isolation.

| Test | What it proves | Why it matters |
|---|---|---|
| `test_first_record_persists_to_the_tenant_stream` | A new event is written to `billing:events:<tenant>`. | Sanity — events reach durable storage. |
| `test_duplicate_idempotency_key_is_not_persisted_twice` | Two posts with the same idempotency key → stream length stays 1. | **Claim-first-then-XADD** — without it, a client retry on network timeout produces phantom billing events. |
| `test_different_idempotency_keys_both_persist` | Distinct keys are not conflated. | Obvious-in-retrospect check that the key is actually used. |
| `test_idempotency_is_scoped_per_tenant` | Tenant-a and tenant-b can both use `"shared-key"` — neither dedupes the other. | Global idempotency would let tenants silently nuke each other's events by collision. |
| `test_streams_are_keyed_by_tenant` | Tenant-x and tenant-y's events live in distinct Redis streams. | Precondition for tenant-scoped XLEN/XRANGE queries at billing time. |
| `test_idempotency_marker_has_ttl` | The dedup marker carries a bounded 7-day TTL. | Redis memory doesn't grow forever. |

### 3.4 `tests/unit/test_tenant_plan.py` (9 tests, 5 parametrised — all `[fix-correctness]`)

Pure-data catalog of tiers and tenant mappings.

| Test | What it proves | Why it matters |
|---|---|---|
| `test_known_tenants_map_to_their_plan` | globex→enterprise, initech→standard, acme-corp→backfill, piedmont→small. | A typo here would route a customer to the wrong limits — billing and capacity both. |
| `test_unknown_tenant_falls_back_to_default` | An unknown tenant gets the `default` tier, not an error. | Unknown is not a reason to deny service — just to cap it. |
| `test_every_plan_has_all_three_limits` (×5) | Every tier defines positive `requests_per_second`, `burst`, `max_concurrency`. | A missing field would be a silent misconfiguration. |
| `test_enterprise_is_at_least_as_generous_as_small` | enterprise > small on all three axes. | Sanity guard against an accidental swap during an edit. |
| `test_backfill_is_tighter_than_enterprise_on_concurrency` | `backfill.max_concurrency < enterprise.max_concurrency`. | **The policy the incident forced.** A backfilling tenant (acme-corp) cannot be on the same tier as a paying enterprise customer. |

### 3.5 `tests/unit/test_billing_calculator.py` (5 tests — all `[fix-correctness]`)

The billing-calculator use case.

| Test | What it proves | Why it matters |
|---|---|---|
| `test_calculate_writes_a_draft_under_tenant_namespace` | Draft lands at `billing:invoice:<tenant>:<id>` with correct shape. | Namespacing is what makes cross-tenant invoice lookup safe. |
| `test_calculate_falls_back_to_default_pricing` | No per-tenant price configured → default rate applies. | Otherwise a new tenant's first invoice would be 0. |
| `test_calculate_respects_configured_pricing` | Configured rate × event count = expected amount. | The contract we sell to customers. |
| `test_calculate_attaches_ttl_to_draft` | Drafts have a bounded TTL. | Drafts are speculative; if abandoned they must clean up. |
| `test_calculate_is_tenant_isolated` | One tenant's event stream does not inflate another's event_count. | **Tenant isolation at the billing layer** — the incident's root concern applied to invoices. |

### 3.6 `tests/unit/test_invoice_generator.py` (4 tests — all `[fix-correctness]`)

The invoice-generator use case.

| Test | What it proves | Why it matters |
|---|---|---|
| `test_generate_issues_a_draft` | `status=draft` → `status=issued`, stamps `issued_at`. | The primary state transition. |
| `test_generate_raises_when_draft_missing` | Missing invoice raises `InvoiceNotFound` (→ HTTP 404 at the view). | Domain exception surfaces cleanly; no 500s. |
| `test_generate_refreshes_ttl` | Issuing a near-expired draft pushes TTL back to the full hour. | An issued invoice shouldn't vanish seconds after issuance. |
| `test_generate_is_tenant_scoped` | Tenant-attacker cannot issue tenant-owner's draft even knowing the invoice_id. | **Security boundary.** Without it, a tenant could guess/leak an invoice_id and invoice it. |

### 3.7 `tests/integration/test_admission_flow.py` (9 tests — mostly `[api-shape]`, two `[reproduction-adjacent]`)

End-to-end through the Django middleware chain using pytest-django's
synchronous test client.

| Test | Category | What it proves |
|---|---|---|
| `test_ingest_happy_path` | `[api-shape]` | 202 + duplicate=False on a well-formed ingest. |
| `test_ingest_missing_tenant_header_returns_400` | `[api-shape]` | X-Tenant-ID is mandatory. |
| `test_ingest_duplicate_is_deduped` | `[api-shape]` | Retry with same idempotency key returns 202 + duplicate=True. |
| `test_rate_limit_kicks_in_for_small_tenant` | **`[reproduction-adjacent]`** | 80 sequential ingests as piedmont → at least 10 get 429. **Would fail on the buggy system** — it had no rate limiter. |
| `test_rate_limit_response_carries_retry_after_header` | `[api-shape]` | 429 responses carry a Retry-After header. |
| `test_billing_calculate_happy_path` | `[api-shape]` | calculate returns a draft with an invoice_id. |
| `test_billing_calculate_rejects_inverted_period` | `[api-shape]` | period_end < period_start → 400. |
| `test_invoice_generate_round_trip` | `[api-shape]` | calculate → generate round-trip succeeds. |
| `test_invoice_generate_returns_404_for_missing_draft` | `[api-shape]` | Non-existent invoice → 404 with error=`invoice_not_found`. |

### 3.8 `tests/integration/test_incident_reproduction.py` (3 tests — the centrepiece)

These are the tests that speak directly to the assignment rubric:
"tests that prove it behaves correctly under the conditions that
caused this incident."

| Test | Category | What it proves | Would it pass on the buggy system? |
|---|---|---|---|
| `test_noisy_tenant_is_capped_and_quiet_tenant_is_unaffected` | **`[reproduction]`** | Under 40 concurrent heavy calls from a noisy tenant (piedmont, cap=3), admission produces many 429s AND a quiet tenant (globex) sees 100% 202s on its ingests. | **No** — buggy system has no concurrency gate → 0 concurrency-429s → `assert piedmont_429 > 0` fails. |
| `test_without_concurrency_gate_noisy_tenant_is_unbounded` | `[demonstration]` | Monkeypatch the semaphore to always admit; 30 concurrent calls as globex produce >20 admissions. | **Yes** — this test deliberately simulates the bug. It illustrates the pre-fix shape but is not itself a failing-on-bug test. |
| `test_rate_limiter_isolates_noisy_ingester` | **`[reproduction]`** | piedmont (small, burst=50) hits ≥40 rate-limit 429s on 120 ingests; globex (enterprise, burst=1000) sees 120/120 202s. | **No** — buggy system has no rate-limit → 0 429s → `assert piedmont_429 >= 40` fails. |

---

## 4. What this suite does NOT cover (and why)

The assignment mentions these incident symptoms:

- **Queue explosion** (worker-thread queue blows up)
- **Latency spike** (p95/p99 balloon)
- **Redis errors** (timeouts, pool exhausted)
- **Cross-tenant impact**

We cover **cross-tenant impact** directly. The rest we only cover
indirectly (status-code counts rather than the symptom itself).

To close the gap fully you would add, for example:

1. A latency test: wrap every `_post` in `time.perf_counter()`, measure
   globex's p95 during the flood, assert it stays below a sensible
   bound (e.g. 500 ms with the gate on; demonstrate it blows past that
   with the gate neutered).
2. A pool-exhaustion test: configure the Redis client with
   `max_connections=5` and `socket_timeout=0.5s`, run the pre-fix flood
   shape, observe `redis.exceptions.ConnectionError` on the *quiet*
   tenant's calls — then show the same flood with the gate on produces
   zero pool errors.
3. A queue-depth test: instrument `ConcurrencyMiddleware` with a
   per-tenant in-flight gauge; sample it during the flood and assert
   it never exceeds `max_concurrency`.

These extensions are tracked as future work in the top-level
`README.md`.

---

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ConnectionError: Error connecting to redis://localhost:6379/15` | Redis isn't running. See §1. |
| Tests wipe db=15 | That's by design. Set `TEST_REDIS_URL` to another db=15 instance if you want pure isolation. |
| `AssertionError: Refusing to run tests against REDIS_URL=…` | The tripwire fired because `REDIS_URL` doesn't end in `/15`. Use the default or override `TEST_REDIS_URL`. |
| Some test passes on one machine and fails on another, with timing-related asserts (e.g. refill test) | The refill test sleeps 0.7 s; on a pathologically slow CI you might see admitted_after_refill=2 (the lower bound). That's still within the assertion. |
| `live_server` fixture error about `_live_server_modified_settings` | You're importing pytest-django's version; our custom fixture lives in `tests/conftest.py` and carries a no-op shim. Make sure you didn't delete the shim by accident. |

---

## 6. Quick reference — what each test file proves in one line

- `test_token_bucket.py` — atomic rate-limiter math is correct, even under 100-way concurrency.
- `test_concurrency_semaphore.py` — atomic concurrency-slot math is correct, even with crashed holders.
- `test_event_repository.py` — events are idempotent and tenant-scoped.
- `test_tenant_plan.py` — the plan catalog is internally consistent and backfill < enterprise.
- `test_billing_calculator.py` — drafts land at tenant-namespaced keys with bounded TTL; tenant events do not cross.
- `test_invoice_generator.py` — draft → issued transition is safe, tenant-scoped, and refreshes TTL.
- `test_admission_flow.py` — the HTTP contract end-to-end; two of these would fail on the buggy system.
- `test_incident_reproduction.py` — the headline: admission control actually isolates noisy and quiet tenants under concurrent load, and the absence of the gate lets one tenant exceed its cap.
