#!/usr/bin/env python3
"""
build_icd10cm_index.py
======================
Build a dedicated ICD-10-CM database index from UMLS MRCONSO.RRF.

Outputs:
  KB creation v1/database/icd10cm.duckdb
    - icd10cm_codes(cui, code, name, tty)
    - icd10cm_synonyms(cui, term, term_norm)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

KB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KB_ROOT))

from src.utils.text_normalizer import normalize

RRF_PATH = KB_ROOT / "data" / "raw" / "META" / "MRCONSO.RRF"
DB_PATH = KB_ROOT / "database" / "icd10cm.duckdb"


def main():
    if not RRF_PATH.exists():
        print(f"Error: {RRF_PATH} not found.")
        sys.exit(1)

    print(f"Reading from {RRF_PATH}")
    print(f"Target DuckDB database: {DB_PATH}")
    start_total = time.time()

    # Pass 1: Extract all ICD10CM codes where SUPPRESS == 'N'
    print("\n--- Pass 1: Extracting ICD10CM codes ---")
    t0 = time.time()
    icd_cuis = set()
    code_rows = []

    with open(RRF_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "|ICD10CM|" in line:
                parts = line.split("|")
                # parts[0]: CUI, parts[11]: SAB, parts[12]: TTY, parts[13]: CODE, parts[14]: STR, parts[16]: SUPPRESS
                if parts[11] == "ICD10CM" and parts[16] == "N":
                    cui = parts[0]
                    tty = parts[12]
                    code = parts[13]
                    name = parts[14]
                    icd_cuis.add(cui)
                    code_rows.append((cui, code, name, tty))

    print(f"Pass 1 complete in {time.time() - t0:.2f}s")
    print(f"  Found {len(code_rows)} ICD10CM code records across {len(icd_cuis)} distinct CUIs")

    # Pass 2: Extract all English synonyms for these CUIs
    print("\n--- Pass 2: Extracting English synonyms for ICD10CM CUIs ---")
    t0 = time.time()
    synonym_rows = []
    seen = set()

    with open(RRF_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            # Quick substring checks before splitting
            if "|ENG|" in line:
                parts = line.split("|")
                cui = parts[0]
                if cui in icd_cuis and parts[1] == "ENG" and parts[16] == "N":
                    term = parts[14]
                    term_norm = normalize(term)
                    if term_norm:
                        key = (cui, term_norm)
                        if key not in seen:
                            seen.add(key)
                            synonym_rows.append((cui, term, term_norm))

    print(f"Pass 2 complete in {time.time() - t0:.2f}s")
    print(f"  Found {len(synonym_rows)} distinct (cui, term_norm) synonyms")

    # Write to DuckDB
    print("\n--- Writing to DuckDB ---")
    if DB_PATH.exists():
        try:
            DB_PATH.unlink()
        except Exception:
            pass

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE icd10cm_codes (
            cui VARCHAR,
            code VARCHAR,
            name VARCHAR,
            tty VARCHAR
        );
    """)

    con.execute("""
        CREATE TABLE icd10cm_synonyms (
            cui VARCHAR,
            term VARCHAR,
            term_norm VARCHAR
        );
    """)

    # Insert codes
    print("Inserting icd10cm_codes...")
    df_codes = pd.DataFrame(code_rows, columns=["cui", "code", "name", "tty"])
    con.execute("INSERT INTO icd10cm_codes SELECT * FROM df_codes")

    # Insert synonyms
    print("Inserting icd10cm_synonyms...")
    df_syn = pd.DataFrame(synonym_rows, columns=["cui", "term", "term_norm"])
    con.execute("INSERT INTO icd10cm_synonyms SELECT * FROM df_syn")

    # Create indexes
    print("Creating indexes on term_norm, code, and cui...")
    con.execute("CREATE INDEX idx_code ON icd10cm_codes(code);")
    con.execute("CREATE INDEX idx_codes_cui ON icd10cm_codes(cui);")
    con.execute("CREATE INDEX idx_syn_norm ON icd10cm_synonyms(term_norm);")
    con.execute("CREATE INDEX idx_syn_cui ON icd10cm_synonyms(cui);")

    # Verify and print row counts
    count_codes = con.execute("SELECT COUNT(*) FROM icd10cm_codes").fetchone()[0]
    count_synonyms = con.execute("SELECT COUNT(*) FROM icd10cm_synonyms").fetchone()[0]
    con.close()

    total_time = time.time() - start_total
    print("\n========================================================")
    print("ICD-10-CM Index Build Summary:")
    print(f"  Database : {DB_PATH}")
    print(f"  icd10cm_codes row count   : {count_codes:,}")
    print(f"  icd10cm_synonyms row count: {count_synonyms:,}")
    print(f"  Total time: {total_time:.2f}s")
    print("========================================================")


if __name__ == "__main__":
    main()

