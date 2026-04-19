"""
Redis client factory.

Every module that talks to Redis — the event repository, the token
bucket, the (future) concurrency semaphore — goes through this
function. It returns the same ``redis.Redis`` instance for every
caller in the process. Understanding *why* it's a singleton is the
whole point of this file.

What ``redis.Redis`` actually is
--------------------------------
The object returned by ``redis.Redis.from_url(...)`` is **not a
single TCP socket**. It is a thin façade over a ``ConnectionPool``
that manages a set of open TCP connections to Redis (default max
~50). When a caller does ``client.set(...)``, the library:

    1. Checks out an idle connection from the pool.
    2. Writes the command onto the socket.
    3. Reads the reply.
    4. Returns the connection to the pool.

If the pool is empty (all connections in use), the call either
blocks waiting for one to come back or raises
``ConnectionError: too many connections``. That second case is
literally the ``connection pool exhausted`` line in the incident
logs we are solving for.

Why one shared client instead of many
-------------------------------------
If every subsystem built its own ``redis.Redis`` object, every
subsystem would get its own independent ``ConnectionPool``:

    * event_repository   → 50 connections
    * token_bucket       → 50 connections
    * (future) semaphore → 50 connections
    * ...

Redis would then see **150+ simultaneous connections per app
process**, and — worse — there would be no single "pool utilization"
number to watch. One pool could fire ``pool exhausted`` while the
others have idle capacity, and no dashboard would explain why.

Sharing one pool has four concrete benefits that matter for this
codebase:

    1. **Single saturation signal.** When the pool screams, we know
       the app as a whole is overloaded — we don't have to correlate
       across subsystems.
    2. **Fair contention at the boundary.** A rate-limiter check
       and an ingest write compete for the same connections, so a
       misbehaving caller is felt by everyone and becomes visible.
    3. **One knob to tune.** ``max_connections`` is set once; no
       subsystem silently drifts from the others.
    4. **Clean test reset.** ``get_redis_client.cache_clear()``
       rebuilds the one client; no hidden instances to track down.

Why ``decode_responses=True``
-----------------------------
By default ``redis-py`` returns ``bytes``. With this flag, it
returns ``str``. Everything this codebase stores in Redis is text —
JSON, stream field/value pairs, integer counters — so the decode is
always safe, and we avoid the class of bugs where a missed
``.decode("utf-8")`` lets a bytes value flow into a comparison or
into ``json.loads``.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    """Return the shared Redis client for this process.

    The ``@lru_cache(maxsize=1)`` decorator is what makes this a
    singleton: the first caller constructs the ``redis.Redis``
    instance, every subsequent caller — from any module — receives
    the *same object* back. ``maxsize=1`` is also a visual signal
    to a reader: "there is one cached value here, by design."

    Tests that need a clean slate call
    ``get_redis_client.cache_clear()`` to force a reconstruction on
    the next access — useful when swapping the Redis URL for an
    isolated test database.
    """
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
