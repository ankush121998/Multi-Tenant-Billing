"""
Unit tests for the per-tenant concurrency semaphore.

What this file tests
--------------------
The semaphore is the in-flight gate — the second, and incident-
critical, admission control. It limits the number of heavy requests
a single tenant can have executing simultaneously. Implementation is
a Redis ZSET + atomic Lua: each holder is an entry scored by
timestamp; ``acquire`` evicts anything older than ``SLOT_TTL_SECONDS``
(the crashed-worker case), counts the remainder, and only inserts if
below capacity.

Where the incident touches this file
------------------------------------
This is the gate that would have prevented the incident. The
original system capped arrival rate (implicitly, via shared
infrastructure) but not the in-flight count — so a tenant firing a
handful of long-running calls per second would pile up concurrent
in-flight requests, drain the shared Redis pool, and starve unrelated
tenants. Every test here guards a property that had to hold for the
fix to actually work.

Category
--------
All ``[fix-correctness]`` — these verify the mitigation is built
correctly. They do not reproduce the incident; see
``tests/integration/test_incident_reproduction.py`` for that.

How to run
----------
    .venv/bin/python -m  pytest -v -s tests/unit/test_concurrency_semaphore.py
test_concurrent_acquires_never_exceed_capacity

Every test prints a "Scenario" narrative, the observed values, and a
"PASS" line on success.
"""

from __future__ import annotations

import threading
import time

from billing.infrastructure.concurrency_semaphore import (
    SLOT_TTL_SECONDS,
    acquire,
    release,
)


def test_acquire_admits_up_to_capacity():
    """Sequential acquires respect the cap.

    Property: with ``capacity=3``, the first 3 acquires return
    ``admitted=True``; the 4th returns ``admitted=False``.

    Why it matters: the entire story depends on "cap means cap".
    If even one extra acquire slipped through, the semaphore would be
    a lie.

    """
    print("\n[concurrency_semaphore] Scenario: capacity=3. First 3 acquires "
          "admit; 4th must be rejected.")
    handles = [acquire(key="test:sem:a", capacity=3) for _ in range(3)]
    fourth = acquire(key="test:sem:a", capacity=3)
    print(f"  observed: first3_admitted={sum(h.admitted for h in handles)}/3, "
          f"fourth_admitted={fourth.admitted}")
    assert all(h.admitted for h in handles)
    assert fourth.admitted is False
    print("  PASS: semaphore caps in-flight at capacity exactly.")


def test_release_frees_a_slot():
    """Release returns the slot to the pool.

    Property: fill ``capacity=2``; the 3rd acquire is blocked.
    Release one holder; a fresh acquire now succeeds.

    Why it matters: without release, every tenant locks up after
    ``max_concurrency`` long-lived requests, permanently. The middleware
    always releases in a ``finally`` block — this test guards the other
    end of that contract, that release actually frees a slot.

    """
    print("\n[concurrency_semaphore] Scenario: fill capacity=2, confirm 3rd is "
          "blocked, release one holder, confirm a fresh acquire now succeeds.")
    handles = [acquire(key="test:sem:b", capacity=2) for _ in range(2)]
    blocked = acquire(key="test:sem:b", capacity=2)
    release(key="test:sem:b", slot_id=handles[0].slot_id)
    after_release = acquire(key="test:sem:b", capacity=2)
    print(f"  observed: full_blocked={not blocked.admitted}, "
          f"after_release_admitted={after_release.admitted}")
    assert all(h.admitted for h in handles)
    assert blocked.admitted is False
    assert after_release.admitted is True
    print("  PASS: release returns the slot to the pool.")


def test_release_of_unknown_slot_is_harmless():
    """Release of a non-existent slot_id is a safe no-op.

    Property: calling ``release`` with a ``slot_id`` that was
    never acquired does not raise, does not corrupt the counter, and
    does not prevent a subsequent acquire from succeeding.

    Why it matters: the middleware's ``finally`` block may fire
    after an exception partway through ``acquire`` that, e.g., managed
    to write to Redis but not return a handle to Python. We want
    release to tolerate those shapes — otherwise a rare exception
    path turns into "tenant locked out".

    """
    print("\n[concurrency_semaphore] Scenario: releasing a slot_id that was "
          "never acquired must neither raise nor corrupt the counter.")
    release(key="test:sem:c", slot_id="does-not-exist")
    h = acquire(key="test:sem:c", capacity=1)
    print(f"  observed: release_didnt_raise=True, subsequent_admit={h.admitted}")
    assert h.admitted is True
    print("  PASS: unknown-slot release is a no-op.")


def test_stale_slots_self_evict(redis_client):
    """Crashed holders (stale slots) self-evict on next acquire.

    Property: ``zadd`` a "ghost-holder" with a score older than
    ``SLOT_TTL_SECONDS`` to simulate a worker that crashed mid-request
    without releasing its slot. The next ``acquire`` must evict the
    ghost (via ``ZREMRANGEBYSCORE`` inside the Lua script) before
    counting — so we admit the caller even though capacity=1.

    Why it matters: without this, every worker crash would
    permanently reduce the tenant's effective concurrency by 1. Over
    hours/days of operation the tenant would find themselves locked
    out entirely. This is the reason we score slots by timestamp
    rather than storing a count.

    """
    print("\n[concurrency_semaphore] Scenario: simulate a crashed holder by "
          "zadd-ing a 'ghost' slot with a score older than SLOT_TTL_SECONDS. "
          "Next acquire (capacity=1) must self-evict the ghost and admit us.")
    key = "test:sem:d"
    stale_score = time.time() - SLOT_TTL_SECONDS - 5
    redis_client.zadd(key, {"ghost-holder": stale_score})

    h = acquire(key=key, capacity=1)
    ghost_remaining = redis_client.zscore(key, "ghost-holder")
    print(f"  observed: admitted={h.admitted}, in_flight={h.in_flight}, "
          f"ghost_score_after={ghost_remaining}")
    assert h.admitted is True
    assert h.in_flight == 1
    assert ghost_remaining is None
    print("  PASS: ZREMRANGEBYSCORE evicted the stale holder before counting.")


def test_concurrent_acquires_never_exceed_capacity():
    """50-way concurrent acquire never admits more than ``capacity``.

    Property: fire 50 threads at a ``capacity=5`` semaphore. Total
    admitted must be *exactly* 5.

    Why it matters: the atomicity argument. In a non-atomic
    implementation two threads could both read ``in_flight=4``, both
    conclude "there's room", and both insert, admitting 2 callers on
    1 slot. Under 50-way concurrency the overshoot is catastrophic
    — **exactly the runaway-concurrency shape the incident exposed**
    at the infrastructure level. Lua on the Redis server serialises
    everyone.

    """
    print("\n[concurrency_semaphore] Scenario: fire 50 threads at capacity=5 "
          "semaphore. Without Lua atomicity concurrent callers could overshoot; "
          "we assert admitted == 5 exactly.")
    key = "test:sem:e"
    capacity = 5
    thread_count = 50

    admitted_flags: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        h = acquire(key=key, capacity=capacity)
        with lock:
            admitted_flags.append(h.admitted)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    admitted = sum(admitted_flags)
    print(f"  observed: threads={thread_count}, admitted={admitted}, "
          f"capacity={capacity}")
    assert admitted == capacity
    print("  PASS: Lua atomicity prevents overshoot under 50-way concurrency.")


def test_different_keys_are_independent(redis_client):
    """Per-tenant keys isolate the concurrency budget.

    Property: fill tenant-a's cap=3; tenant-b must still be able
    to acquire its own 3 slots.

    Why it matters: this is the isolation property the incident
    exposed the absence of.** If the semaphore used a global key,
    tenant-a's noisy behaviour would consume tenant-b's budget.
    Per-tenant keys (``billing:concurrency:<tenant>``) are the
    structural fix.

    """
    print("\n[concurrency_semaphore] Scenario: fill tenant-a's cap=3, then "
          "confirm tenant-b can still acquire its own 3 slots — keys are "
          "namespaced and independent.")
    tenant_a_admitted = sum(
        acquire(key="test:sem:tenant-a", capacity=3).admitted for _ in range(3)
    )
    tenant_b_admitted = sum(
        acquire(key="test:sem:tenant-b", capacity=3).admitted for _ in range(3)
    )
    print(f"  observed: tenant_a_admitted={tenant_a_admitted}/3, "
          f"tenant_b_admitted={tenant_b_admitted}/3")
    assert tenant_a_admitted == 3
    assert tenant_b_admitted == 3
    print("  PASS: per-tenant keys isolate the noisy neighbour completely.")
