# Multi-Tenant-Billing

# Context:


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