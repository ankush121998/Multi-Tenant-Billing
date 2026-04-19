"""
Load runner: visually reproduce the noisy-neighbor incident and its fix.

What this script does
---------------------
Fires two concurrent workloads against a running instance of this
service:

  (1) Noisy tenant — a configurable tenant floods a heavy
      endpoint (``/billing/calculate`` by default) with many
      concurrent workers. This is the shape of acme-corp's backfill.

  (2) Quiet tenant — a different, unrelated tenant performs
      ordinary ingests at a relaxed pace. This is globex / initech
      during the incident.

The script prints per-tenant outcome counts: how many requests
succeeded, how many hit the rate-limit gate (429 ``rate_limited``),
how many hit the concurrency gate (429 ``concurrency_limit``), and
how many failed for other reasons.

How to interpret the output
---------------------------
With the server configured as-is (both middlewares on):
    * the noisy tenant sees many ``concurrency_limit`` 429s — the
      fix working,
    * the quiet tenant sees ~100% 2xx — tenants are isolated.

To see the pre-fix behaviour, edit ``billing_platform/settings.py``
and comment out ``ConcurrencyMiddleware``. Re-run this script:
    * the noisy tenant admits far more concurrent calls,
    * the quiet tenant may start seeing slowdowns or failures under
      heavy enough load (pool pressure from the noisy tenant).

Usage
-----
    python scripts/simulate_incident.py
    python scripts/simulate_incident.py --noisy acme-corp --quiet globex \\
        --noisy-requests 200 --noisy-concurrency 20 --quiet-requests 50

The server must already be running:
    python manage.py runserver 8000

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field


@dataclass
class Outcome:
    """One request's result, classified."""

    status: int
    kind: str  # "success", "rate_limited", "concurrency_limit", "error"
    error_body: dict | None = None


@dataclass
class TenantStats:
    """Aggregated stats for one tenant across all its calls."""

    tenant: str
    endpoint: str
    outcomes: list[Outcome] = field(default_factory=list)
    wall_clock_seconds: float = 0.0

    def summary(self) -> dict[str, int]:
        c: Counter[str] = Counter(o.kind for o in self.outcomes)
        c["total"] = len(self.outcomes)
        return dict(c)


def _classify(status: int, body: dict | None) -> str:
    if 200 <= status < 300:
        return "success"
    if status == 429 and body:
        err = body.get("error", "")
        if err == "rate_limited":
            return "rate_limited"
        if err == "concurrency_limit":
            return "concurrency_limit"
    return "error"


def _post(url: str, tenant: str, body: dict, timeout: float = 10.0) -> Outcome:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Tenant-ID": tenant},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return Outcome(status=resp.status, kind=_classify(resp.status, parsed))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return Outcome(
            status=e.code, kind=_classify(e.code, parsed), error_body=parsed,
        )
    except (urllib.error.URLError, TimeoutError) as e:
        return Outcome(status=0, kind="error", error_body={"exception": str(e)})


def _flood_heavy_endpoint(
    base_url: str, tenant: str, endpoint: str, n: int, concurrency: int,
) -> TenantStats:
    url = f"{base_url}{endpoint}"
    body = {
        "customer_id": "cust-1",
        "period_start": "2026-04-01T00:00:00Z",
        "period_end": "2026-05-01T00:00:00Z",
    }
    stats = TenantStats(tenant=tenant, endpoint=endpoint)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_post, url, tenant, body) for _ in range(n)]
        for f in as_completed(futures):
            stats.outcomes.append(f.result())
    stats.wall_clock_seconds = time.perf_counter() - start
    return stats


def _stream_ingests(
    base_url: str, tenant: str, n: int, concurrency: int,
) -> TenantStats:
    url = f"{base_url}/api/v1/events/ingest"
    stats = TenantStats(tenant=tenant, endpoint="/api/v1/events/ingest")

    def call(i: int) -> Outcome:
        body = {
            "event_name": "api_call",
            "customer_id": "cust-1",
            "timestamp": "2026-04-01T12:00:00Z",
            "idempotency_key": f"sim-{tenant}-{i}-{time.time_ns()}",
            "value": 1,
        }
        return _post(url, tenant, body)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(call, i) for i in range(n)]
        for f in as_completed(futures):
            stats.outcomes.append(f.result())
    stats.wall_clock_seconds = time.perf_counter() - start
    return stats


def _print_stats(label: str, stats: TenantStats) -> None:
    s = stats.summary()
    total = s.get("total", 0)
    success = s.get("success", 0)
    rl = s.get("rate_limited", 0)
    cc = s.get("concurrency_limit", 0)
    err = s.get("error", 0)
    pct = (success / total * 100.0) if total else 0.0
    print(f"  {label:<7} tenant={stats.tenant!r:<14} endpoint={stats.endpoint}")
    print(
        f"          total={total:<4} success={success:<4} "
        f"rate_limited={rl:<4} concurrency_limit={cc:<4} error={err:<4} "
        f"({pct:.1f}% success, {stats.wall_clock_seconds:.2f}s wall)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--noisy", default="acme-corp",
                        help="tenant to use for the flood")
    parser.add_argument("--noisy-endpoint", default="/api/v1/billing/calculate",
                        help="heavy endpoint to flood")
    parser.add_argument("--noisy-requests", type=int, default=60)
    parser.add_argument("--noisy-concurrency", type=int, default=15)
    parser.add_argument("--quiet", default="globex",
                        help="unrelated tenant doing quiet work")
    parser.add_argument("--quiet-requests", type=int, default=30)
    parser.add_argument("--quiet-concurrency", type=int, default=5)
    args = parser.parse_args()

    print(f"== Simulating noisy-neighbor against {args.host}")
    print(f"   noisy: {args.noisy!r} → {args.noisy_endpoint}"
          f" × {args.noisy_requests} req, {args.noisy_concurrency} workers")
    print(f"   quiet: {args.quiet!r} → /api/v1/events/ingest"
          f" × {args.quiet_requests} req, {args.quiet_concurrency} workers")
    print()

    # Run both tenants in parallel so the flood and the quiet traffic
    # overlap — that's what makes the isolation story visible.
    with ThreadPoolExecutor(max_workers=2) as top:
        noisy_future = top.submit(
            _flood_heavy_endpoint,
            args.host, args.noisy, args.noisy_endpoint,
            args.noisy_requests, args.noisy_concurrency,
        )
        quiet_future = top.submit(
            _stream_ingests,
            args.host, args.quiet,
            args.quiet_requests, args.quiet_concurrency,
        )
        noisy_stats = noisy_future.result()
        quiet_stats = quiet_future.result()

    print("Results")
    print("-------")
    _print_stats("NOISY", noisy_stats)
    _print_stats("QUIET", quiet_stats)
    print()

    quiet_success_rate = (
        sum(1 for o in quiet_stats.outcomes if o.kind == "success")
        / max(1, len(quiet_stats.outcomes))
    )
    if quiet_success_rate >= 0.99:
        print("Tenant isolation holding: the quiet tenant was unaffected.")
    else:
        print(
            f"WARNING — quiet tenant success rate is "
            f"{quiet_success_rate:.1%}. Either the quiet tenant is overloaded "
            f"or the concurrency middleware is disabled — check settings."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
