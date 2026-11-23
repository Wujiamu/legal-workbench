"""TEST-ONLY fixtures. Real PostgreSQL is required; no SQLite or in-memory DB."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from legal_workbench.app import app
from legal_workbench.config import load_settings
from legal_workbench.db import configure_engine, get_engine, reset_engine, sqlalchemy_url
from legal_workbench.storage import Storage

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "legal_workbench_test"
# The HTTP layer refuses writes without an Origin naming loopback, and the test
# client must use the same base URL the app validates Host against.
BASE = "http://127.0.0.1:8765"


def _test_urls() -> tuple[str, str]:
    settings = load_settings()
    migrator = make_url(settings.migration_url)
    app = make_url(settings.database_url)
    migrator_test = migrator.set(database=TEST_DB_NAME)
    app_test = app.set(database=TEST_DB_NAME)
    return (
        migrator_test.render_as_string(hide_password=False),
        app_test.render_as_string(hide_password=False),
    )


@pytest.fixture(scope="session")
def test_database() -> tuple[str, str]:
    """Recreate the test database from empty and migrate it with the real chain."""
    migrator_url, app_url = _test_urls()
    admin_url = make_url(migrator_url).set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    env = dict(os.environ, LWB_MIGRATION_URL=migrator_url, LWB_DATABASE_URL=app_url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed: {result.stderr}")
    yield migrator_url, app_url
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migration_engine(test_database):
    engine = create_engine(sqlalchemy_url(test_database[0]), future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def bind_test_engine(test_database):
    """Point the process-wide engine at the throwaway test database.

    Only the ``db`` fixture used to switch the engine, so any test that called
    ``get_session_factory()`` directly — the worker, the pipeline, R01-R04 —
    connected to whatever ``LWB_DATABASE_URL`` named, i.e. the business database.
    Binding for the whole session is what makes the suite safe to run against a
    database whose tables do not exist.
    """
    configure_engine(test_database[1])
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def clean_db(migration_engine):
    """Every test starts from empty business tables and an inactive maintenance flag."""
    with migration_engine.begin() as conn:
        tables = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename NOT IN ('alembic_version', 'maintenance_state')"
        )).scalars().all()
        if tables:
            conn.execute(text("TRUNCATE TABLE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE"))
        conn.execute(text(
            "INSERT INTO maintenance_state (id, active) VALUES (1, false) "
            "ON CONFLICT (id) DO UPDATE SET active=false, reason=NULL"
        ))


@pytest.fixture
def db(test_database):
    """Code under test connects with the application role (no DDL rights).

    The engine is already bound to the test database for the whole session; this
    fixture must not reset it, or later direct ``get_session_factory()`` calls
    would fall back to the configured business database.
    """
    session = Session(bind=get_engine())
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def app_engine(test_database):
    engine = create_engine(sqlalchemy_url(test_database[1]), future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def store(tmp_path) -> Storage:
    """Managed storage rooted in the test's temporary directory."""
    return Storage(root=tmp_path / "data")


@pytest.fixture
def app_database_url(test_database) -> str:
    return test_database[1]


@pytest.fixture
def migration_database_url(test_database) -> str:
    return test_database[0]


@pytest.fixture
def synthetic_fixtures_dir() -> Path:
    """Return the TEST-ONLY fixture directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def api(db, tmp_path):
    """A client bound to the test database and a temporary data root."""
    app.state.storage = Storage(root=tmp_path / "data")
    with TestClient(app, base_url=BASE) as client:
        yield client
    app.state.storage = None


@pytest.fixture
def client(api):
    api.headers["Origin"] = BASE
    return api
