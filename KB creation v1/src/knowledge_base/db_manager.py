"""DuckDB connection management, schema initialization and run bookkeeping."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

from config.settings import SETTINGS
from src.utils.ids import new_run_id

log = logging.getLogger(__name__)
SCHEMA_VERSION = "2"


class SchemaError(RuntimeError):
    pass


def connect(db_path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(db_path or SETTINGS.paths.database)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def initialize_schema(con: duckdb.DuckDBPyConnection, schema_sql: Path | None = None) -> None:
    sql = Path(schema_sql or SETTINGS.paths.schema_sql).read_text(encoding="utf-8")
    con.execute(sql)
    found = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if not found or found[0] != SCHEMA_VERSION:
        raise SchemaError(
            f"Database schema_version={found[0] if found else None} but code expects {SCHEMA_VERSION}. "
            "Back up the .duckdb file and re-run scripts/initialize_database.py --recreate."
        )


def require_schema(con: duckdb.DuckDBPyConnection) -> None:
    try:
        v = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    except duckdb.CatalogException as exc:
        raise SchemaError("Schema not initialized. Run: python scripts/initialize_database.py") from exc
    if not v or v[0] != SCHEMA_VERSION:
        raise SchemaError(f"Unexpected schema version {v}; expected {SCHEMA_VERSION}.")


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection):
    con.execute("BEGIN TRANSACTION")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def insert_records(con: duckdb.DuckDBPyConnection, table: str, records: list[dict], columns: Iterable[str] | None = None) -> int:
    """Bulk insert list-of-dicts via a pandas DataFrame (much faster than executemany)."""
    if not records:
        return 0
    df = pd.DataFrame.from_records(records, columns=list(columns) if columns else None)
    for col in df.columns:  # JSON columns: serialize dict/list values that are not list-typed SQL columns
        if df[col].map(lambda v: isinstance(v, dict)).any():
            df[col] = df[col].map(lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, dict) else v)
    # Cast every column to the target column's declared type: columns that are all NULL or
    # contain only empty lists would otherwise be inferred as type NULL and fail to insert.
    types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
                             [table]).fetchall())
    exprs = []
    for c in df.columns:
        t = types.get(c)
        if t and t.endswith("[]"):
            # lists travel as JSON text and are rebuilt with the declared element type
            df[c] = df[c].map(lambda v: None if v is None else json.dumps(list(v), default=str))
            exprs.append(f"""from_json("{c}", '["{t[:-2]}"]')""")
        elif t:
            exprs.append(f'CAST("{c}" AS {t})')
        else:
            exprs.append(f'"{c}"')
    con.register("_ins_df", df)
    try:
        cols = ", ".join(df.columns)
        sel = ", ".join(exprs)
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {sel} FROM _ins_df")
    finally:
        con.unregister("_ins_df")
    return len(df)


def current_umls_release(con: duckdb.DuckDBPyConnection) -> str | None:
    row = con.execute("SELECT value FROM schema_meta WHERE key='active_umls_release'").fetchone()
    return row[0] if row else None


def kb_is_loaded(con: duckdb.DuckDBPyConnection) -> bool:
    return bool(con.execute("SELECT COUNT(*) FROM cpt_codes").fetchone()[0])


def loaded_code_sabs(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT DISTINCT sab FROM cpt_codes").fetchall()}


class Run:
    """Context manager recording a processing run (status, stats, errors)."""

    def __init__(self, con, run_type: str, umls_release: str | None = None, input_file: str | None = None,
                 input_sha256: str | None = None):
        self.con, self.run_type = con, run_type
        self.run_id = new_run_id(run_type)
        self.stats: dict = {}
        self.umls_release, self.input_file, self.input_sha256 = umls_release, input_file, input_sha256

    def __enter__(self):
        self.con.execute(
            "INSERT INTO processing_runs (run_id, run_type, status, started_at, processing_version, umls_release,"
            " input_file, input_sha256, settings_json) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)",
            [self.run_id, self.run_type, datetime.now(), SETTINGS.processing_version, self.umls_release,
             self.input_file, self.input_sha256, json.dumps(SETTINGS.as_dict())],
        )
        log.info("Started run %s", self.run_id)
        return self

    def __exit__(self, exc_type, exc, tb):
        status = "failed" if exc_type else "succeeded"
        self.con.execute(
            "UPDATE processing_runs SET status=?, finished_at=?, stats_json=?, error_message=?, umls_release=? WHERE run_id=?",
            [status, datetime.now(), json.dumps(self.stats, default=str), None if not exc else f"{exc_type.__name__}: {exc}",
             self.umls_release, self.run_id],
        )
        (log.error if exc_type else log.info)("Run %s %s", self.run_id, status)
        return False
