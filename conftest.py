"""Shared pytest fixtures.

Integration tests run against a real PostgreSQL database (``agentos_test``) on
the docker compose Postgres instance; SQLite is never used. The database is
recreated and migrated once per session and truncated between tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
PG_HOST = os.environ.get("AGENTOS_TEST_PG", "localhost:55432")
TEST_DB = os.environ.get("AGENTOS_TEST_DB", "agentos_test")
TEST_DB_URL = f"postgresql+asyncpg://agentos:agentos@{PG_HOST}/{TEST_DB}"
ARTIFACT_DIR = Path(tempfile.mkdtemp(prefix="agentos-test-artifacts-"))

os.environ.update(
    {
        "AGENTOS_ENVIRONMENT": "test",
        "AGENTOS_DATABASE_URL": TEST_DB_URL,
        "AGENTOS_REDIS_URL": os.environ.get("AGENTOS_TEST_REDIS", "redis://localhost:56379/15"),
        "AGENTOS_ARTIFACT_DIR": str(ARTIFACT_DIR),
        "AGENTOS_LEASE_TTL_SECONDS": "2",
        "AGENTOS_HEARTBEAT_INTERVAL_SECONDS": "0.3",
        "AGENTOS_POLL_INTERVAL_SECONDS": "0.1",
        "AGENTOS_COORDINATOR_INTERVAL_SECONDS": "0.2",
        "AGENTOS_LOG_JSON": "false",
        "AGENTOS_LOG_LEVEL": "WARNING",
        "AGENTOS_RATE_LIMIT_PER_MINUTE": "100000",
        "AGENTOS_CRITICAL_RATE_LIMIT_PER_MINUTE": "100000",
    }
)
os.environ.pop("ANTHROPIC_API_KEY", None) if os.environ.get("AGENTOS_LIVE_TESTS") != "1" else None
os.environ.pop("OPENAI_API_KEY", None) if os.environ.get("AGENTOS_LIVE_TESTS") != "1" else None

import pytest  # noqa: E402

_db_ready: bool | None = None


class MigrationError(RuntimeError):
    """A failing migration must fail the session, never skip the integration tests."""


_lock_handle = None


def _acquire_session_lock() -> None:
    """Only one pytest session may own ``agentos_test``: a second session would DROP the database
    under the first (pg_terminate_backend), which presents as a hang or a mid-test connection error."""
    import fcntl

    global _lock_handle
    _lock_handle = open(Path(tempfile.gettempdir()) / f"agentos-pytest-{TEST_DB}.lock", "w")  # noqa: SIM115 - held for the whole session
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError(
            f"another pytest session is already using database {TEST_DB!r}; wait for it to finish "
            "(or set AGENTOS_TEST_DB to a different name)"
        ) from None


def _create_database() -> None:
    import asyncio

    import asyncpg

    _acquire_session_lock()

    async def _go() -> None:
        conn = await asyncpg.connect(f"postgresql://agentos:agentos@{PG_HOST}/postgres")
        try:
            await conn.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{TEST_DB}'"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()

    asyncio.run(_go())
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "apps/api/alembic.ini"), "upgrade", "head"],
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTOS_DATABASE_URL": TEST_DB_URL},
    )
    if migrate.returncode != 0:
        raise MigrationError(migrate.stderr[-2000:])


@pytest.fixture(scope="session")
def database() -> str:
    global _db_ready
    if _db_ready is None:
        try:
            _create_database()
            _db_ready = True
        except (MigrationError, RuntimeError):
            raise
        except Exception as exc:  # pragma: no cover - environment dependent
            _db_ready = False
            pytest.skip(
                f"PostgreSQL not available at {PG_HOST}: {exc} (run: docker compose up -d postgres redis)"
            )
    if not _db_ready:
        pytest.skip("PostgreSQL not available")
    return TEST_DB_URL


TABLES = (
    "events, model_usage, artifacts, approval_requests, tool_executions, step_attempts, workflow_steps, "
    "workflow_runs, workflow_definitions, agent_definitions, document_chunks, documents, idempotency_records, "
    "workers, workspace_members, workspaces, users"
)


@pytest.fixture
async def db(database: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from agentos_core.db.session import get_sessionmaker
    from agentos_core.notify import set_notifier

    set_notifier(None)
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await session.commit()
    yield get_sessionmaker()


@pytest.fixture
async def seeded(db):  # type: ignore[no-untyped-def]
    from agentos_core.testing import seed_demo

    return await seed_demo()


@pytest.fixture
async def arena_seeded(seeded):
    from sqlalchemy import select

    from agentos_core.arena.contenders import seed_arena
    from agentos_core.db.models import AgentDefinition, Workspace
    from agentos_core.engine.tx import unit_of_work

    async with unit_of_work() as s:
        await seed_arena(s, await s.get(Workspace, seeded.workspace.id), admin_id=None)
    async with unit_of_work() as s:
        for a in (
            await s.execute(
                select(AgentDefinition).where(AgentDefinition.workspace_id == seeded.workspace.id)
            )
        ).scalars():
            seeded.agents[a.name] = a
    return seeded


# ----------------------------------------------------------------------------- offline proof for the whole suite
_OUTBOUND: list[str] = []


@pytest.fixture(autouse=True, scope="session")
def no_outbound_connections():  # type: ignore[no-untyped-def]
    """Every test in the session runs with outbound (non-loopback) socket connections blocked and recorded.

    The suite needs only local PostgreSQL and Redis. A connection to any other host (a model provider, a telemetry
    backend, anything) raises immediately, and the attempt is also remembered so a test that swallowed the error still
    fails the session. This is what lets CI run with no paid-provider credentials and proves that nothing in the product's
    tested paths contacts an external service."""
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else str(address)
        if (
            isinstance(host, str)
            and host not in ("127.0.0.1", "::1", "localhost")
            and not host.startswith("/")
        ):
            _OUTBOUND.append(f"{host}")
            raise ConnectionRefusedError(
                f"outbound connection to {host!r} blocked by the test suite (offline guarantee)"
            )
        return real_connect(self, address)

    socket.socket.connect = guarded  # type: ignore[method-assign]
    yield
    socket.socket.connect = real_connect  # type: ignore[method-assign]
    assert not _OUTBOUND, f"the suite attempted outbound connections to: {sorted(set(_OUTBOUND))}"
