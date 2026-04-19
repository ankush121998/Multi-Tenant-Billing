My Understanding:
    1 - The root problem is shared-state contention in a multi-tenant system, not a random application bug.
    2 - Build a tenant-aware control mechanism.
            The best fit is some mix of per-tenant limits, fairness, prioritization, backpressure, and admission control. In simple words: stop one tenant’s backfill from consuming all shared Redis connections or worker slots.
    3 - Test should show the bad case: under heavy acme-style backfill, globex/initech/piedmont start timing out or failing. Then the fixed version should show isolation   and recovery.

What the logs actually say (root-cause reasoning)
    Walking the timeline, not just the error lines:

    03:17:38–43 — acme-corp is pounding /events/ingest at ~1–2 req/s, every request 11–22 ms, 200 OK. Their requests are individually healthy. This matches the context clue: acme migrated 6 weeks ago and is running a historical-events backfill.
    In parallel, globex runs /billing/calculate: 234 ms → 891 ms → 2103 ms. The same endpoint is monotonically slowing down. Nothing about globex's own workload explains that.
    03:17:43 — first cross-tenant damage: globex and initech ingests jump to 4.8 s and 5.1 s. Acme's requests in the same second stay at 18 ms.
    03:17:44 — first 504s (globex ingest 7841 ms, initech billing 9200 ms).
    03:17:45 — connection pool exhausted on globex billing. This is the confirmation: the shared connection pool (likely DB, possibly Redis client pool) is drained.
    03:17:46–49 — redis timeout after 5000ms cascades across globex, initech. Then 503 service unavailable. piedmont (200 ev/day, tiny) still gets 200s through this window because its arrival rate is low enough to find a free slot. acme keeps getting 200s — because its requests are cheap and synchronous, it effectively got "first in line" for every resource.

    Root cause: 
        this is the noisy-neighbor problem on shared infrastructure. Acme's backfill isn't expensive per-request, but its aggregate arrival rate saturates the shared Redis connection pool and ECS worker concurrency. Once pool utilization crosses a threshold, any tenant whose request wants a costly operation (/billing/calculate) queues behind acme's flood, blows past the 5 s Redis client timeout, and the failure cascades laterally to other tenants — including the SLA customer whose cycle closes today, and the prospect whose CFO is the champion.

    Key nuance the logs reveal:

        Acme is not "malicious" or even unusual for their current phase — a backfill is a legitimate workload. The platform is just not isolating them.
        The damage is not proportional to blame. Acme causes it, globex/initech feel it. That's the textbook noisy-neighbor failure mode and it's what the solution has to invert.
        The problem is admission, not compute. By the time a request holds a Redis connection for 5 s, it's too late — we should have rejected or deferred it earlier.

The narrowest credible solution would be: 
    An ingest path, a billing path and Invoice path where all uses Redis, a way to classify traffic by tenant and by workload type, a fairness layer that limits or schedules work per tenant, real Redis-backed state, end-to-end tests that recreate the saturation.

From that, what needs to be built:
    1 - Per-tenant admission control on the API (Django middleware), enforced before any Redis / DB / downstream work is done. Token-bucket or sliding-window counter, state in Redis itself (with a Lua script for atomicity and to avoid its own race). Rejects with 429 and a Retry-After header — fail fast, not at the 5 s Redis timeout.

    2 - Tenant tiers / plans, config-driven, so globex (enterprise, SLA) has higher and reserved capacity vs. piedmont (small) vs. acme (currently in migration/backfill). The tier must be loadable at runtime (Redis-backed, not code-deployed) so oncall can lift a tier in an incident without a deploy.

    3 - Endpoint cost weighting. /events/ingest is cheap — 1 token. /billing/calculate and /invoices/generate are heavy — consume more tokens from the same bucket, or have a separate "heavy" bucket. This is what prevents acme's cheap ingest flood from starving globex's heavy billing call.
    
    4 - Per-tenant Redis connection / concurrency cap. Rate limits cap arrival; they don't cap simultaneous in-flight cost. A semaphore (Redis-backed counter) per tenant on the shared resource prevents any single tenant from holding more than N of the pool's connections at once — this is the direct fix for "connection pool exhausted."


What I would rule out:
    Just increase Redis size — that only delays the problem.
    Just add more ECS tasks — shared contention can still collapse the system.
    Increase Connection Pool size - High queue depth would still occur, so no benefit of increasing Connection pool size
    Global rate limit only — that punishes well-behaved tenants too.
    Pure retries — retries usually make saturation worse.
    Dedicated infra for every tenant — probably too expensive and contrary to the shared-tenant setup described.


Key intuition:
    It’s better to reject early than to accept and fail later under load
    Because:
        late failure wastes CPU, memory, Redis connections
        late failure impacts all tenants
        early failure isolates damage


Work type prioritization:
    From logs:
        /events/ingest → high volume, low priority (especially backfill) → holds lesser number of Redis connections as compared to billing & invoice functions
        /billing/calculate → critical
        /invoices/generate → critical


# Key Explanations of what i understood from the Redis setup and how it's operating

1 - The platform runs on shared ECS clusters. Redis is shared across all tenants."
        That means one Redis instance (or one Redis Cluster) serves acme, globex, initech, piedmont — all of them. There is no per-tenant Redis. Every XADD, every SET, every INCR from every tenant hits the same Redis process.

2 - Redis processes commands from all clients through one FIFO-ish queue. Acme's commands and globex's commands wait in the same line.

3 - Redis operation's latency has TWO parts
    total latency for one op = wait_time + service_time
                                ↑           ↑
                                time spent   time Redis actually
                                in the       takes to execute
                                queue        your command
                                waiting
                                your turn

    Service time = how long Redis takes to actually run your command once it starts. For a SET or XADD, this is microseconds — typically 50–200 µs. It is essentially constant regardless of load. Redis doesn't slow down on a per-command basis.

    Wait time = how long your command sits in the queue before Redis picks it up. Under light load this is ~0. Under heavy load it grows. It can grow to seconds.

4 - Application-side Redis connection pool
        Each Django/ECS worker process holds a client-side pool of Redis connections — typically something like 20–50(assumption) open TCP sockets to Redis, reused across requests.

        When the view code does r.set(...), it:
            1 - Checks a connection out of the pool.
            2 - Writes the command to the socket.
            3 - Waits for the response.
            4 - Returns the connection to the pool.
            5 - A connection is "held" for the duration of that wait — usually < 1 ms when Redis is idle.

        Under acme's flood: the pool still has 20 sockets, but each one is held much longer than normal (explained in point 5 below). When all 20 are held at once, the 21st request has nothing to check out. It either blocks waiting for one to come back, or errors immediately if the pool is configured to fail-fast.

        "connection pool exhausted" is that second case — the pool ran out of sockets and the client gave up.


5 - Redis server's command queue
    
        Here's the bit that answers "why does each connection get held longer?"

        Redis has one thread. Commands from all clients land in that one thread's work queue. Normally:

        acme sends 510 ops/sec
        globex sends 50 ops/sec
        initech sends 20 ops/sec
        piedmont sends a few
        Total: ~250 ops/sec. Redis handles each in microseconds. Nothing queues up meaningfully. Every client sees ~1 ms round-trip.

        Under acme's backfill flood:
            acme is now sending, say, 5,000 ops/sec (backfills fire hard — they're catching up months of data).
            globex and initech are still sending their normal trickle.
            Redis is still fast per command, but the queue is now deep. When globex's /billing/calculate needs to do 5 Redis operations, each of those 5 operations now has to wait behind thousands of acme's operations. What was 5 × 1 ms = 5 ms becomes 5 × 30 ms = 150 ms. Then 5 × 200 ms = 1 second. Then it blows past the 5-second client timeout and you see "redis timeout after 5000ms".

        This is exactly what the log shows for globex's billing calls:

        234 ms → 891 ms → 2103 ms → 7841 ms 504 → connection pool exhausted
        Monotonic slowdown — classic queue-buildup signature.

5 - How the Redis Server's command queue depth increase poisons Application-side Redis connection pool
        Because Redis ops now take 200 ms instead of 1 ms, the application holds each connection in its pool for 200× longer. A pool that comfortably served 500 req/sec now saturates at 2–3 req/sec. New requests can't get a connection, so they time out and log "connection pool exhausted".

        The cascade:
            Acme's flood
                → Redis server command queue grows
                → each Redis op takes 100×+ longer
                → application holds pool connections 100×+ longer
                → pool exhausts
                → new requests fail before they even reach Redis
                → 503 service unavailable
            That's why globex and initech, who weren't even sending much traffic, got 503s. They were never the problem — they were just waiting in the same line.

        Why acme's individual requests stayed fast
            03:17:43 tenant=acme-corp ... duration=18ms  status=200
            03:17:44 tenant=acme-corp ... duration=14ms  status=200
            03:17:45 tenant=acme-corp ... duration=15ms  status=200

        Acme is fine. Globex is dying. Why?

        Acme's individual operations (one XADD and one SET per request) are cheap — a few Redis commands each. Acme's request enters the Redis queue, waits for some of its own 5000-op/sec flood to drain, but only needs its 2 ops done. So it still comes back fast relative to its cost budget.
        Globex's /billing/calculate needs, say, 20 Redis ops. Each of those ops sits in the same queue acme is filling. 20 × queue-wait = hugely amplified.
        Acme is both the cause and the most-protected caller. That's the noisy-neighbor failure: damage is not proportional to fault.

        A second effect: cheap request = connection held briefly even under load. Acme's connections cycle through the pool quickly. Globex's heavy call holds its connection for the full duration of its 20 slow Redis ops, occupying one of the precious 20 pool slots for the entire span.

        What "acme's backfill caused the blast" means concretely
            Backfill = "I just migrated to your platform, I'm importing 6 weeks of historical usage events so my invoices are correct." The migration notes say acme joined 6 weeks ago.

        Normal steady-state for acme might be ~50 events/sec. Backfill rate is "as fast as the client script can fire" — could easily be 1,000–10,000 events/sec, sustained for hours. That's the flood.

        Every single one of those events is a write that goes through the shared Redis (idempotency SET NX, stream XADD, probably more). So acme is pinning the Redis command thread to ~100% utilization with its own traffic.

        The backfill isn't "malicious" or even unusual — it's legitimate migration workload. The platform just has no mechanism to say "acme can use at most X% of the shared Redis capacity at any moment, so other tenants always have room." That mechanism is what the assignment is asking us to build.

        Why the fix isn't "just add more Redis"
            A few ways people reflexively "solve" noisy neighbors and why none of them alone fix this:

                Vertical scale Redis — bigger box doesn't help because command execution is single-threaded. The bottleneck core is unchanged.

                Redis Cluster (sharding) — helps if you shard by tenant. But a single hot tenant (acme) still pins one shard. Unless you shard within a tenant's keyspace, acme still dominates the shard that holds their data.

                Bigger connection pool — just delays the cliff. Holding 200 connections instead of 20 means Redis is now flooded with 10× the concurrent commands, making the queue worse. You trade pool-exhaustion errors for more severe timeouts. Increasing number of connections results into a higher queue depth, redis is still serving the commands from queue one by one, so it wouldn't help solve the problem.

                Async everything — kicks the problem one layer down but the underlying resource contention doesn't disappear.

                What actually fixes it: admission control — stop acme at the front door before they can touch Redis, using a budget they're allowed to consume. That's the rate limiter + per-tenant concurrency cap i am building in this assignment. The shared infrastructure stays shared; I just give each tenant a guaranteed floor and a ceiling they can't punch through.