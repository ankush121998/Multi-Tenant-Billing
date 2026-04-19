"""
Root-level pytest conftest.

Sets ``REDIS_URL`` to the test database (db=15) at the earliest
possible import moment, before pytest-django loads
``billing_platform.settings``. Belt-and-suspenders: ``tests/conftest.py``
also re-forces ``settings.REDIS_URL`` and clears the client
singleton on every test, so the suite is safe even if this hook is
skipped by a plugin-ordering quirk.

Test fixtures themselves (flush-between, tripwire assertion,
``redis_client`` alias) live in ``tests/conftest.py``.
"""

from __future__ import annotations

import os

os.environ["REDIS_URL"] = os.environ.get(
    "TEST_REDIS_URL", "redis://localhost:6379/15"
)
