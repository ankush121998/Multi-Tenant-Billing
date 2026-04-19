"""
Redis client factory.

A single, process-wide client is returned for every caller. The
underlying ``redis.Redis`` instance is a thin handle over a
connection pool — sharing it lets every subsystem (event
persistence, rate limiter state, concurrency semaphores) benefit
from the same pool and makes saturation observable as a single
signal.

``decode_responses=True`` means callers work with ``str`` instead of
``bytes``. All callers in this codebase handle JSON or stream
entries, not raw binary, so the decoding is safe and removes a
class of encode/decode bugs.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    """Return the shared Redis client for this process.

    Cached so every call site shares the same connection pool.
    Reset only in tests via ``get_redis_client.cache_clear()``.
    """
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
