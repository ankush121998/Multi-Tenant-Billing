"""
Test harness setup.

Redis isolation
---------------
Every test runs against ``redis://localhost:6379/15`` — a dedicated
logical DB reserved for tests. Real Redis is used (the assignment
forbids mocks): the fixtures below FLUSHDB that database before each
test so tests never see state leaked from their neighbours, and an
assertion guards against anyone accidentally running the suite
against a non-test DB.

We override ``settings.REDIS_URL`` in the fixture rather than
relying only on env-var ordering: pytest-django loads Django settings
during plugin initialisation, which happens before per-directory
conftests. Mutating ``settings.REDIS_URL`` plus clearing the
``get_redis_client`` singleton is the reliable override point — it
works no matter when env vars were read.
"""

from __future__ import annotations

import os

os.environ["REDIS_URL"] = os.environ.get(
    "TEST_REDIS_URL", "redis://localhost:6379/15"
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "billing_platform.settings")

import pytest
import redis as redis_lib
from django.conf import settings

from billing.infrastructure.redis import get_redis_client


_TEST_REDIS_URL = os.environ["REDIS_URL"]


def _assert_test_db(url: str) -> None:
    """Refuse to run if REDIS_URL isn't the test DB.

    Flushing a dev/prod DB mid-test would be a catastrophe; we'd
    rather explode loudly at the first test than silently wipe
    someone's local state.
    """
    assert url.endswith("/15") or "/15" in url.rsplit("?", 1)[0], (
        f"Refusing to run tests against REDIS_URL={url!r}; "
        "tests must target the dedicated test DB (db=15)."
    )


@pytest.fixture(autouse=True)
def _flush_redis_between_tests():
    """Force the test Redis URL, FLUSHDB before and after every test."""
    # Override settings and clear the client singleton so the next
    # ``get_redis_client()`` call picks up the test URL. Necessary
    # because pytest-django loaded settings.py before this conftest.
    settings.REDIS_URL = _TEST_REDIS_URL
    get_redis_client.cache_clear()

    _assert_test_db(settings.REDIS_URL)

    client = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()
    get_redis_client.cache_clear()


@pytest.fixture
def redis_client(_flush_redis_between_tests):
    """Alias: tests that want direct Redis access use this fixture."""
    return _flush_redis_between_tests


# ---------------------------------------------------------------------------
# Live HTTP server fixture for integration tests.
#
# We don't use pytest-django's ``live_server`` because under Django 5 +
# Python 3.12 its static-files handler trips over a bytes-vs-str bug
# (``path.startswith first arg must be str or a tuple of str, not bytes``).
# The admission-control tests don't need static files at all — just a
# real threaded WSGI server. Rolling our own keeps it boring.
# ---------------------------------------------------------------------------


import socket
import threading
from dataclasses import dataclass, field


class _NoopModifiedSettings:
    """Shim for pytest-django's ``_live_server_helper`` autouse hook.

    pytest-django unconditionally calls
    ``live_server._live_server_modified_settings.enable()/disable()``
    whenever a test requests the ``live_server`` fixture. We don't
    need those settings tweaks (DEBUG propagation, ALLOWED_HOSTS), so
    we expose a no-op to satisfy the interface.
    """

    def enable(self) -> None:  # noqa: D401
        pass

    def disable(self) -> None:  # noqa: D401
        pass


@dataclass
class _LiveServer:
    url: str
    _live_server_modified_settings: _NoopModifiedSettings = field(
        default_factory=_NoopModifiedSettings,
    )


@pytest.fixture
def live_server():
    """Start Django's ``ThreadedWSGIServer`` on a free port, yield its URL.

    Threaded WSGI lets concurrent HTTP clients actually run in parallel
    (the admission-control concurrency tests depend on that).
    """
    from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler
    from django.core.wsgi import get_wsgi_application

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = get_wsgi_application()

    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, *args, **kwargs):  # silence per-request logs
            pass

    server = ThreadedWSGIServer(("127.0.0.1", port), _QuietHandler)
    server.set_app(app)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _LiveServer(url=f"http://127.0.0.1:{port}")
    finally:
        server.shutdown()
        server.server_close()
