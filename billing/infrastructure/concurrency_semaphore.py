"""
Per-tenant concurrency semaphore backed by Redis.

Why this exists (and why it complements the rate limiter)
---------------------------------------------------------
The token bucket caps arrival rate: "this tenant may not start
more than N requests per second." It does not cap how many
requests can be in flight at the same time. Those two limits are
different. A tenant that fires 5 calls per second, where each call
takes 10 seconds to run, is well under any sane rate limit — and yet
at steady state they have ~50 concurrent requests pinning 50 Django
worker threads and 50 Redis connections.

That is precisely the shape of the incident this codebase is solving:
acme-corp's backfill started many long-running /billing/calculate
jobs that each kept a Redis connection held open for the duration.
The connection pool exhausted, and every other tenant (globex,
initech) started seeing 5s timeouts on unrelated ingests.

The concurrency semaphore is the second admission gate. Before a heavy
endpoint runs, we ask Redis: "does this tenant have a free slot?"
If not, 429 — the request never holds a worker, never holds a
connection. The cap is per-tenant (plan-defined: enterprise=20,
small=3, backfill=5, ...) so one tenant running hot can fill its
own slots without starving anyone else's.

Why a Redis sorted set (ZSET) and not a counter
-----------------------------------------------
The naive design — "INCR on acquire, DECR on release" — breaks the
first time a worker crashes mid-request. The counter drifts upward,
and after enough crashes the tenant is permanently locked out even
though nothing is actually running. We'd need an operator to zero
the key by hand.

A sorted set solves this elegantly. Each acquire inserts a unique
slot_id with the current timestamp as the score. Release does
ZREM slot_id. Before every acquire, we first evict any slot whose
score is older than SLOT_TTL_SECONDS — those are assumed crashed
and their slot is reclaimed. So:

    * normal path: acquire adds, release removes, count is exact
    * crash path: the stale entry self-evicts after the TTL window
    * the semaphore is self-healing; no operator intervention needed

The TTL is chosen longer than any realistic request duration but
short enough that a crashed slot is reclaimed in the same billing
cycle (60s is comfortable — our heaviest endpoint runs in <5s).

Why Lua (atomicity)
-------------------
"Evict stale → count current → maybe insert" must be one atomic unit.
If it weren't, two concurrent callers could both read count == N-1,
both pass the check, and both insert — leaving the tenant one over
the cap. Redis runs Lua scripts under its single command thread, so
the whole sequence runs with no interleaving. Classic use of
CAS-style atomicity without actually needing CAS.

Release is a single ZREM and does not need a script.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import cast

from billing.infrastructure.redis import get_redis_client


# Longer than the slowest realistic heavy-endpoint call (<5s today),
# short enough that a crashed holder's slot frees well within the
# billing cycle. 60s hits both.
SLOT_TTL_SECONDS = 60

# KEYS[1] = sorted-set key for this tenant
# ARGV[1] = capacity (int)
# ARGV[2] = now (float seconds, app-supplied — Redis TIME also works,
#           but we already have the app clock and the drift doesn't
#           matter for a TTL-scale check)
# ARGV[3] = stale cutoff (float seconds; entries with score <= cutoff
#           are considered crashed and evicted before the count)
# ARGV[4] = slot_id to insert if admitted
# ARGV[5] = expiry seconds for the whole key (so an idle tenant's
#           semaphore key doesn't leak memory forever)
#
# Returns {admitted, in_flight_after}.
_ACQUIRE_LUA = """
local capacity = tonumber(ARGV[1])
local now      = tonumber(ARGV[2])
local cutoff   = tonumber(ARGV[3])
local slot_id  = ARGV[4]
local ttl      = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local in_flight = redis.call('ZCARD', KEYS[1])

if in_flight >= capacity then
    redis.call('EXPIRE', KEYS[1], ttl)
    return {0, in_flight}
end

redis.call('ZADD', KEYS[1], now, slot_id)
redis.call('EXPIRE', KEYS[1], ttl)
return {1, in_flight + 1}
"""


@dataclass(frozen=True)
class SlotHandle:
    """Opaque receipt returned by ``acquire``.

    Holders pass this back to ``release`` to give up their slot. The
    ``admitted`` flag tells the caller whether they hold a slot at all —
    on ``False`` the caller must not proceed into the guarded work, and
    must not call ``release`` (releasing a slot you don't hold would
    just no-op, but it's a signal something is wrong).
    """

    admitted: bool
    slot_id: str
    in_flight: int


def acquire(*, key: str, capacity: int) -> SlotHandle:
    """Try to take a concurrency slot for ``key``.

    Atomic on Redis: eviction, count check, insertion all happen inside
    one Lua run, so two concurrent callers cannot both see "one slot
    free" and both pass.
    """
    client = get_redis_client()
    now = time.time()
    slot_id = uuid.uuid4().hex
    raw = cast(
        list,
        client.eval(
            str(_ACQUIRE_LUA),
            1,
            key,
            str(capacity),
            f"{now:.6f}",
            f"{now - SLOT_TTL_SECONDS:.6f}",
            slot_id,
            str(SLOT_TTL_SECONDS),
        ),
    )
    return SlotHandle(
        admitted=int(raw[0]) == 1,
        slot_id=slot_id,
        in_flight=int(raw[1]),
    )


def release(*, key: str, slot_id: str) -> None:
    """Give up a previously acquired slot.

    A single ZREM — no script needed. If the slot was already evicted
    by the stale-cleanup path (holder was slower than ``SLOT_TTL_SECONDS``),
    this is a harmless no-op.
    """
    client = get_redis_client()
    client.zrem(key, slot_id)
