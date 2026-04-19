"""
Atomic token-bucket rate limiter backed by Redis.

The bucket's entire state — current tokens and last-refill timestamp —
lives in one Redis hash. A single Lua script reads the state, refills
based on elapsed time, checks whether ``cost`` tokens are available,
consumes them (or not), and writes the state back. Because Redis runs
Lua scripts atomically under its single command thread, two concurrent
callers cannot observe the same "free tokens" and both succeed: the
second caller always sees the first caller's consumption.

Time comes from Redis's own ``TIME`` command rather than the app's
wall clock. Multiple app hosts would otherwise disagree on "now" by
milliseconds (NTP skew), and those milliseconds directly translate to
unfair refill amounts. One Redis = one clock.

Keys expire an hour after the last touch so idle tenants don't leak
memory; the bucket is recreated at full capacity on next use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from billing.infrastructure.redis import get_redis_client


_BUCKET_TTL_SECONDS = 3600

# KEYS[1] = bucket hash key
# ARGV[1] = capacity            (max tokens the bucket can hold)
# ARGV[2] = refill_rate         (tokens added per second)
# ARGV[3] = cost                (tokens this request wants to consume)
#
# Returns {allowed, tokens_remaining, retry_after_seconds}, all as strings.
_LUA_SCRIPT = """
local capacity    = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local cost        = tonumber(ARGV[3])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state       = redis.call('HMGET', KEYS[1], 'tokens', 'last_refill')
local tokens      = tonumber(state[1]) or capacity
local last_refill = tonumber(state[2]) or now

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

if tokens < cost then
    redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    local retry_after = (cost - tokens) / refill_rate
    return {0, tostring(tokens), tostring(retry_after)}
end

tokens = tokens - cost
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {1, tostring(tokens), '0'}
"""


@dataclass(frozen=True)
class BucketDecision:
    """Outcome of one ``consume`` call.

    ``tokens_remaining`` reflects the bucket *after* the decision.
    On a reject, it's what the caller saw at check time — useful for
    telling a client "you had 2.3 tokens, you needed 10".
    """

    allowed: bool
    tokens_remaining: float
    retry_after_seconds: float


def consume(
    *,
    key: str,
    capacity: int,
    refill_rate: float,
    cost: float,
) -> BucketDecision:
    """Try to consume ``cost`` tokens from the bucket at ``key``.

    A single atomic Redis round-trip. Safe under arbitrary concurrency.
    """
    client = get_redis_client()
    raw = cast(
        list,
        client.eval(
            str(_LUA_SCRIPT),
            1,
            key,
            str(capacity),
            str(refill_rate),
            str(cost),
            str(_BUCKET_TTL_SECONDS)
        ),
    )
    return BucketDecision(
        allowed=int(raw[0]) == 1,
        tokens_remaining=float(raw[1]),
        retry_after_seconds=float(raw[2]),
    )
