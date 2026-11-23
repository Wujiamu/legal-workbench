"""One-time data backfill after migration 0005: default progress fragments (K14).

Run as the application role (DML only). Idempotent: cases that already have
progress nodes are skipped. Invoked by scripts/lwb migrate after alembic upgrade.
"""
from __future__ import annotations

import sys

from .db import session_scope
from . import progress


def main() -> int:
    with session_scope() as session:
        filled = progress.backfill_default_fragments(session)
    print(f"progress backfill: {filled} case(s) instantiated")
    return 0


if __name__ == "__main__":
    sys.exit(main())