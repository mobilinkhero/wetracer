"""Regression guards for the 2026-11-25 AI-config wipe.

On 2026-11-24/25 the Media Test Org's AI config vanished mid-evening while
pytest runs were active, and live widget chat started refusing with
ai_config_missing. Two holes made that possible:

1. The unit harness sets ENV_FILE=/dev/null, so settings.DATABASE_URL falls
   back to the LIVE dev Postgres — and SessionLocal() is bound at import to
   that engine. Patching database.engine (the older fixture) does not
   rebind it, so any app code calling SessionLocal() directly reads/writes
   LIVE data during unit tests. tests/conftest.py now rebinds it; this
   test fails loudly if that ever regresses.
2. The live streaming fixture used to switch the org's ai_configs row to
   the stub and restore it afterwards (and delete the stub row it had
   inserted). That mutates shared live state. tests_live/
   test_streaming_live.py must never contain an ai_configs write again;
   its fixture is read-only and skips unless the live-verification agent
   has already pointed the config at the stub.
"""

from pathlib import Path

import pytest

from app import database


def test_session_local_is_bound_to_the_test_database():
    """SessionLocal() must resolve to an in-memory SQLite test database,
    even though settings.DATABASE_URL points at the live dev Postgres.

    NOTE: do not compare engine identity with tests.conftest.engine here:
    pytest loads that conftest twice (as both `tests.conftest` and
    `backend.tests.conftest`), so two engine objects exist. The fixtures
    — and this patch — consistently use one of them (verified: the `db`
    fixture's bind IS the patched SessionLocal's bind). What matters is
    the dialect: sqlite, never postgresql."""
    session = database.SessionLocal()
    try:
        bind = session.get_bind()
    finally:
        session.close()
    assert bind.dialect.name == "sqlite"
    assert "postgres" not in bind.dialect.name


def test_live_streaming_tests_never_write_ai_configs():
    """The live suite must not UPDATE/INSERT/DELETE ai_configs rows.
    The AI config belongs to the owner / the live-verification agent;
    the live fixture only reads it and skips when it is not the stub."""
    live_file = (
        Path(__file__).resolve().parent.parent
        / "tests_live" / "test_streaming_live.py"
    )
    text = live_file.read_text()
    for forbidden in (
        "update ai_configs",
        "insert into ai_configs",
        "delete from ai_configs",
    ):
        assert forbidden not in text.lower(), (
            f"{live_file.name} must not contain {forbidden!r} — "
            "the live fixture is read-only (see its docstring)"
        )
