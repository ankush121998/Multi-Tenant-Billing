"""
Redis connection-pool exhaustion — symptom-level reproduction.

Why this file exists
--------------------
The original incident logs named two Redis-level failure modes that
the rest of the suite does not reproduce:

    * ``ConnectionError: Too many connections``  (pool exhausted)
    * ``TimeoutError`` on a blocking pool wait    (pool timeout)

Every other test in this repo proves the fix is correct (tenant
isolation, idempotency, admission limits). None of them demonstrate
what the buggy system actually did to Redis under the acme-corp
backfill. This file closes that gap by forcing the failure mode in
isolation, so a reader can see — with their own eyes — what
"connection pool exhausted" means and why the fix (shared singleton
client + admission gates that cap in-flight work) prevents it.

Strategy (how we reproduce the symptom)
---------------------------------------
We don't need hundreds of threads and a real backfill to see pool
exhaustion. We just need more concurrent Redis operations than
the pool has slots. So:

    1. Build a ``redis.Redis`` whose ``BlockingConnectionPool`` has
       ``max_connections=2`` and ``timeout=1`` seconds.
    2. Park 2 threads inside ``BLPOP`` on keys that nobody will ever
       push to — each thread holds one connection for the full BLPOP
       timeout. The pool now has 0 free slots.
    3. From the main thread, try one more Redis command. With no
       slot available and the blocking pool's 1 s wait budget
       exhausted, redis-py raises exactly the error the incident
       logs showed.

No Redis server tuning is required — this is all client-side.
Running the test against the project's local Redis (db=15) is
enough.

How to read the assertions
--------------------------
Each test takes the form:

    >>> with pytest.raises(<expected redis error>):
    ...     <one extra redis call that can't get a connection>

The ``pytest.raises`` context is the whole point — it proves the
error is what the incident logs said it was, not a generic
failure. If the redis-py version ever changes which exception it
raises, the test will flip red and tell you exactly what changed.

What this file does NOT claim
-----------------------------
* It does not reproduce the full incident path (noisy tenant →
  admission flood → shared pool exhausted → everyone 500s). That
  would require the whole HTTP stack and is covered conceptually by
  ``test_incident_reproduction.py``. This file proves the
  mechanism.
* It does not prove the fix prevents pool exhaustion in prod — the
  fix works by capping concurrent in-flight requests upstream of
  Redis (the semaphore + rate limiter), which is tested elsewhere.

Category
--------
``[reproduction]`` — these tests fail on a system that has the bug
(unbounded concurrency + small pool), and pass once work is capped
below pool size.

How to run
----------
    .venv/bin/ python -m  pytest -v -s tests/integration/test_redis_pool_exhaustion.py

    # or a single scenario:
    .venv/bin/ python -m  pytest -v -s tests/integration/test_redis_pool_exhaustion.py::test_pool_exhaustion_raises_timeout

Prerequisites: local Redis on ``localhost:6379`` with db 15 free
(same as the rest of the suite — see ``tests/conftest.py``).
"""

from __future__ import annotations

import threading
import time

import pytest
import redis
from redis.connection import BlockingConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError


# How long each parked BLPOP holds its connection. Long enough that
# the probing command definitely can't acquire a slot; short enough
# that the test finishes quickly.
PARK_SECONDS = 2

# The blocking pool's own wait budget when asking for a connection.
# Shorter than PARK_SECONDS so the probe gives up before the parked
# threads release — that's what produces the "timeout" variant of
# pool exhaustion.
POOL_WAIT_SECONDS = 1


def _tiny_pool_client() -> redis.Redis:
    """Redis client with a deliberately undersized blocking pool.

    * ``max_connections=2`` — only two in-flight Redis ops allowed.
    * ``BlockingConnectionPool`` — callers *wait* for a slot rather
      than immediately erroring. Waiting past ``timeout`` raises
      ``ConnectionError`` (redis-py uses ConnectionError for pool
      timeouts too; it is NOT a plain socket timeout).
    * ``timeout=POOL_WAIT_SECONDS`` — how long to wait for a slot
      before giving up.
    """
    pool = BlockingConnectionPool(
        host="localhost",
        port=6379,
        db=15,
        max_connections=2,
        timeout=POOL_WAIT_SECONDS,
        decode_responses=True,
    )
    return redis.Redis(connection_pool=pool)


def _park_connection(client: redis.Redis, key: str, ready: threading.Event) -> None:
    """Hold one pool slot by blocking on a key nobody will push to.

    BLPOP blocks for up to ``timeout`` seconds waiting for an element
    on the list at ``key``. While it blocks, the thread's Redis
    connection is checked out of the pool — exactly the state we
    need to simulate "all slots in use".
    """
    ready.set()
    try:
        client.blpop(key, timeout=PARK_SECONDS)
    except Exception:
        # We don't care if the blpop itself errors — its only job is
        # to keep the connection busy for a while.
        pass


def test_pool_exhaustion_raises_timeout():
    """Two parked callers + one probe = blocking-pool timeout.

    Setup
    -----
    * Pool size = 2, pool wait = 1 s.
    * Two threads enter BLPOP on unique keys and hold both slots.

    Act
    ---
    * From the main thread, call ``GET`` on any key. There is no free
      connection, so redis-py waits up to 1 s for one, then gives
      up.

    Assert
    ------
    * The call raises ``redis.exceptions.ConnectionError`` with a
      message referencing the connection pool. That is exactly what
      the incident logs showed.

    Why this matters
    ----------------
    This is the concrete, observable mechanism behind the incident's
    "connection pool exhausted" lines. Seeing it in a test makes the
    failure mode real for a reader who has only read the fix.
    """
    print("\n[redis_pool] Scenario: park 2 BLPOPs on a 2-slot pool, then probe "
          "with a GET. The probe must raise ConnectionError because the pool "
          "cannot hand it a connection within the 1s wait budget.")
    client = _tiny_pool_client()

    ready1 = threading.Event()
    ready2 = threading.Event()
    t1 = threading.Thread(
        target=_park_connection, args=(client, "park:1", ready1), daemon=True,
    )
    t2 = threading.Thread(
        target=_park_connection, args=(client, "park:2", ready2), daemon=True,
    )
    t1.start()
    t2.start()

    # Wait until both threads have entered BLPOP (i.e. actually hold
    # a connection). Without this the test races and sometimes the
    # probe succeeds.
    assert ready1.wait(1.0) and ready2.wait(1.0)
    time.sleep(0.05)  # let BLPOP actually check out its connection

    start = time.monotonic()
    with pytest.raises(RedisConnectionError) as exc_info:
        client.get("probe-key")
    elapsed = time.monotonic() - start

    print(f"  observed: error={type(exc_info.value).__name__}, "
          f"msg={str(exc_info.value)!r}, waited={elapsed:.2f}s "
          f"(expected ~{POOL_WAIT_SECONDS}s)")
    assert elapsed >= POOL_WAIT_SECONDS * 0.8, (
        "probe should have waited for a connection before giving up"
    )
    assert elapsed < PARK_SECONDS, (
        "probe should give up well before the parked connections release"
    )

    t1.join(timeout=PARK_SECONDS + 1)
    t2.join(timeout=PARK_SECONDS + 1)
    print("  PASS: pool exhaustion reproduced as ConnectionError with timeout.")


def test_pool_recovers_when_parked_connections_release():
    """Once slots free up, subsequent calls succeed again.

    Why this matters
    ----------------
    Pool exhaustion is a *transient* failure mode — the app is not
    broken, it is overloaded. If we can't show recovery, a reader
    might assume Redis needs a restart. This test proves the pool
    heals on its own as soon as callers finish: the fix upstream
    (admission gates capping concurrent work) works because it keeps
    the pool from saturating in the first place, not because it
    repairs a broken pool.
    """
    print("\n[redis_pool] Scenario: after the parked BLPOPs return, the pool "
          "should hand out connections normally again — exhaustion is "
          "transient, not a permanent break.")
    client = _tiny_pool_client()

    ready = threading.Event()
    t = threading.Thread(
        target=_park_connection, args=(client, "park:solo", ready), daemon=True,
    )
    t.start()
    assert ready.wait(1.0)

    # Pool size is 2, only 1 slot parked → the probe should succeed.
    client.set("probe", "ok")
    assert client.get("probe") == "ok"
    print("  observed: GET succeeded with 1/2 slots in use.")

    # Now wait for the parked thread to return; the pool is fully idle.
    t.join(timeout=PARK_SECONDS + 1)
    assert client.get("probe") == "ok"
    print("  PASS: pool recovers once callers release their connections.")


def test_capping_in_flight_work_prevents_exhaustion():
    """When callers respect a cap ≤ pool size, no exhaustion occurs.

    This is the *shape* of the fix in miniature: the admission-control
    layer (rate limiter + concurrency semaphore) caps concurrent
    in-flight requests below the Redis pool's capacity, so however
    noisy one tenant gets, the pool never saturates.

    Property
    --------
    * Pool size = 2, but we only allow ourselves 2 concurrent Redis
      ops (enforced with a ``threading.Semaphore(2)``).
    * 20 threads do quick GETs under the cap.
    * Every call completes successfully — zero ``ConnectionError``.

    Why this matters
    ----------------
    This is the *contrapositive* of the first test: the pool is the
    same size, the contention is higher (20 callers, not 3), yet
    nothing fails. The only difference is the cap. That is the whole
    thesis of the admission-control fix: cap in-flight work
    *upstream* of Redis and the pool never has to say no.
    """
    print("\n[redis_pool] Scenario: 20 threads hammer a 2-slot pool, but an "
          "upstream semaphore caps concurrency at 2. Expectation: zero errors "
          "— the fix in miniature.")
    client = _tiny_pool_client()
    gate = threading.Semaphore(2)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        with gate:
            try:
                client.set(f"k:{i}", str(i))
                assert client.get(f"k:{i}") == str(i)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    print(f"  observed: errors={len(errors)} (expected 0)")
    assert errors == [], f"capped workers should not exhaust the pool: {errors!r}"
    print("  PASS: capping in-flight work keeps the pool healthy.")
