"""Reset the development database to empty business tables.

Only for the local dev database, which holds synthetic TEST-ONLY data. Keeps the
schema, alembic_version and maintenance_state row intact.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from sqlalchemy import text  # noqa: E402

from legal_workbench.config import load_settings  # noqa: E402
from legal_workbench.db import sqlalchemy_url  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402


def main() -> int:
    # TRUNCATE needs table ownership: connect as the migrator role, not the app role.
    engine = create_engine(sqlalchemy_url(load_settings().migration_url), future=True)
    with engine.begin() as connection:
        tables = connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename NOT IN ('alembic_version', 'maintenance_state')"
        )).scalars().all()
        if tables:
            connection.execute(text(
                "TRUNCATE TABLE " + ", ".join(f'"{name}"' for name in tables) + " CASCADE"))
        connection.execute(text(
            "INSERT INTO maintenance_state (id, active) VALUES (1, false) "
            "ON CONFLICT (id) DO UPDATE SET active=false, reason=NULL"))
    print(f"truncated {len(tables)} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
