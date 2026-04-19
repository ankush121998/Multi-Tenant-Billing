"""
Unit tests for the atomic token-bucket rate limiter.

What this file tests
--------------------
The token bucket is the arrival-rate gate: it decides whether a
tenant's next request is admitted based on how many tokens are in the
tenant's bucket. Tokens refill at a configured rate; the bucket has a
ceiling (``burst``) so an idle tenant cannot accumulate unbounded
capacity. The whole check is a Lua script so it's atomic under
concurrent callers — that atomicity is load-bearing for the incident
story.

Where the incident touches this file
------------------------------------
The incident exposed the fact that a single flooding tenant could
monopolise shared Redis capacity. A rate limiter alone does not fix
that (see ``test_concurrency_semaphore.py`` for the in-flight gate),
but it **is** the first filter: it stops the arrival rate from
overwhelming even the concurrency gate.

Category
--------
All tests here are ``[fix-correctness]`` — they verify the mitigation
is built correctly. They do not reproduce the incident directly;
instead they prove the foundation the incident fix stands on.

How to run
----------
    # just this file
    .venv/bin/ python -m  pytest -v -s tests/unit/test_token_bucket.py

    # one test
    .venv/bin/ python -m  pytest -v -s tests/unit/test_token_bucket.py::\
test_concurrent_consumers_cannot_overdraw_capacity

    # all tests
    .venv/bin/ python -m  pytest -v -s tests/unit/test_token_bucket.py

Every test prints a "Scenario" narrative, the observed values, and a
"PASS" line on success. The narrative tells you exactly what property
the test is verifying — read it if you're trying to understand what
the test does without reading the code.
"""

from __future__ import annotations

import threading
import time

from billing.infrastructure.token_bucket import consume


def test_first_consume_is_allowed_on_a_fresh_bucket():
    """A brand-new bucket must initialise at full capacity and admit.

    Property: when a bucket key does not yet exist in Redis, the
    first ``consume`` admits the call and decrements tokens by ``cost``.

    Why it matters: if the bucket were lazily empty, a tenant's
    very first request of the day would be rejected. That would be a
    reliability bug independent of the incident.
    """
    print("\n[token_bucket] Scenario: brand-new bucket admits the first request "
          "and decrements tokens by the request's cost.")
    decision = consume(key="test:b1", capacity=10, refill_rate=1, cost=1)
    print(f"  observed: allowed={decision.allowed}, "
          f"tokens_remaining={decision.tokens_remaining}")
    assert decision.allowed is True
    assert decision.tokens_remaining == 9
    print("  PASS: fresh bucket initialised to full capacity and consumed 1 token.")


def test_consumes_until_empty_then_rejects():
    """Bucket drains after ``capacity`` admissions, then rejects with retry-after.

    Property: after the bucket is drained, the next ``consume``
    returns ``allowed=False`` AND a positive ``retry_after_seconds``
    hint.

    Why it matters: the rejection must come with a backoff hint so
    clients can honor it and stop retrying. Without a retry-after hint,
    a rate-limited client retry-storms, which makes the incident worse,
    not better.
    """
    print("\n[token_bucket] Scenario: drain a 10-capacity bucket with 10 "
          "sequential calls, then prove the 11th is rejected with a retry-after.")
    for _ in range(10):
        assert consume(key="test:b2", capacity=10, refill_rate=0.1, cost=1).allowed

    rejected = consume(key="test:b2", capacity=10, refill_rate=0.1, cost=1)
    print(f"  observed 11th call: allowed={rejected.allowed}, "
          f"retry_after={rejected.retry_after_seconds:.3f}s")
    assert rejected.allowed is False
    assert rejected.retry_after_seconds > 0
    print("  PASS: bucket empties after exactly capacity admissions and "
          "rejects further calls.")


def test_refills_at_the_configured_rate():
    """Tokens are returned to the bucket at the configured refill rate.

    Property: drain a bucket, wait 0.7 s with ``refill_rate=5/s``
    (i.e. ~3 tokens regenerated), then at least 2 out of 4 further
    consumes must succeed.

    Why it matters: a bucket that only drains is a denial of
    service. Refill is how we convert "burst" into "sustained".

    """
    print("\n[token_bucket] Scenario: drain a 5-capacity bucket, wait 0.7s with "
          "refill_rate=5/s (~3 tokens should regenerate), then confirm further "
          "admissions are possible.")
    for _ in range(5):
        consume(key="test:b3", capacity=5, refill_rate=5, cost=1)
    time.sleep(0.7)

    allowed_after_refill = 0
    for _ in range(4):
        if consume(key="test:b3", capacity=5, refill_rate=5, cost=1).allowed:
            allowed_after_refill += 1
    print(f"  observed: admitted_after_refill={allowed_after_refill} "
          f"(expected >= 2)")
    assert allowed_after_refill >= 2
    print("  PASS: refill rate is honoured — roughly 3 tokens returned in 0.7s.")


def test_capacity_is_an_upper_bound_on_tokens():
    """Long-idle buckets do not accumulate tokens above ``capacity``.

    Property: after 0.2 s idle on a ``capacity=3, refill_rate=100``
    bucket, the next sequence of consumes admits exactly 3 — not the
    theoretical 20 from ``elapsed * rate``.

    Why it matters: without the clamp, a tenant who hasn't called
    in hours could burst past their plan by orders of magnitude as
    soon as they came back. The whole point of ``burst`` as a ceiling
    is predictability.

    """
    print("\n[token_bucket] Scenario: long-idle bucket with refill_rate=100 "
          "should NOT accumulate above capacity — elapsed*rate must be clamped.")
    consume(key="test:b4", capacity=3, refill_rate=100, cost=1)
    time.sleep(0.2)  # would "refill" 20 tokens if uncapped

    admitted = sum(
        consume(key="test:b4", capacity=3, refill_rate=100, cost=1).allowed
        for _ in range(5)
    )
    print(f"  observed: admitted_after_long_idle={admitted} (expected exactly 3)")
    assert admitted == 3
    print("  PASS: Lua caps tokens at capacity; no unbounded growth while idle.")


def test_concurrent_consumers_cannot_overdraw_capacity():
    """100-way concurrent race on one bucket admits exactly ``capacity``.

    Property: fire 100 threads at a ``capacity=20, refill_rate=0``
    bucket. The total number admitted must be *exactly* 20 — never
    more.

    Why it matters: this is the whole reason the check is a Lua
    script. In a read-modify-write implementation, two threads could
    both read ``tokens=5`` and both decrement to ``4``, admitting 2
    callers on 1 token. Under 100-way concurrency the overshoot is
    many. Lua runs atomically on the Redis server, serialising every
    caller.
    """
    print("\n[token_bucket] Scenario: fire 100 threads at a capacity=20 bucket "
          "with refill_rate=0. Without Lua atomicity a race could admit more "
          "than 20. We assert admitted == 20 exactly.")
    key = "test:b5"
    capacity = 20
    refill_rate = 0.0
    cost = 1
    thread_count = 100

    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        d = consume(key=key, capacity=capacity, refill_rate=refill_rate, cost=cost)
        with results_lock:
            results.append(d.allowed)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    admitted = sum(results)
    print(f"  observed: threads={thread_count}, admitted={admitted}, "
          f"capacity={capacity}")
    assert admitted == capacity
    print("  PASS: atomic Lua serialises all 100 concurrent consumers; "
          "no overdraw.")


def test_cost_weighting_subtracts_more_than_one():
    """Per-endpoint cost weighting is respected.

    Property: with ``cost=10`` and ``capacity=10``, the first call
    drains the bucket; the second is rejected.

    Why it matters: not every request costs the same amount of
    work. ``/events/ingest`` does one Redis op; ``/billing/calculate``
    does many. If every request cost one token, a flood of cheap
    ingests and a flood of heavy calculates would look identical to
    the rate limiter — which is wrong. The cost map in
    ``rate_limiter.py`` attaches different weights; this test proves
    the Lua respects them.

    """
    print("\n[token_bucket] Scenario: capacity=10, cost=10 per call. "
          "First call must succeed and drain the bucket; second must be rejected.")
    first = consume(key="test:b6", capacity=10, refill_rate=0, cost=10)
    second = consume(key="test:b6", capacity=10, refill_rate=0, cost=10)
    print(f"  observed: first.allowed={first.allowed}, "
          f"second.allowed={second.allowed}")
    assert first.allowed
    assert second.allowed is False
    print("  PASS: endpoint cost weighting (cost > 1) is respected.")
