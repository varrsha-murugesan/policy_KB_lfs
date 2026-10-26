"""Create the DuckDB database file and apply database/schema.sql (idempotent).

Usage:
    python scripts/initialize_database.py              # create or upgrade-in-place (no data loss)
    python scripts/initialize_database.py --recreate   # delete the database file first (asks no questions)
"""
import argparse

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import connect, initialize_schema
from src.utils.logging_config import setup_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(SETTINGS.paths.database), help="DuckDB file path")
    ap.add_argument("--recreate", action="store_true", help="Delete the existing database file first")
    args = ap.parse_args()
    log = setup_logging("initialize_database", SETTINGS.paths.logs_dir)
    from pathlib import Path
    db = Path(args.db)
    if args.recreate and db.exists():
        db.unlink()
        wal = db.with_suffix(db.suffix + ".wal")
        if wal.exists():
            wal.unlink()
        log.info("Deleted existing database %s", db)
    con = connect(db)
    initialize_schema(con)
    tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1").fetchall()]
    log.info("Schema ready in %s (%d tables/views): %s", db, len(tables), ", ".join(tables))
    con.close()


if __name__ == "__main__":
    main()
