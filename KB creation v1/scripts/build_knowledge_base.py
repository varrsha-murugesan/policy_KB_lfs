"""Load UMLS RRF files from data/raw/META into the DuckDB knowledge base.

Resumable: completed steps are skipped; a failed step is rolled back and
redone on the next run.

Usage:
    python scripts/build_knowledge_base.py                      # uses data/raw/META, auto-detects release
    python scripts/build_knowledge_base.py --release 2026AA
    python scripts/build_knowledge_base.py --with-attributes    # also load CPT full descriptions from MRSAT (slow)
    python scripts/build_knowledge_base.py --force terms        # redo one step (and everything after it)
    python scripts/build_knowledge_base.py --replace            # switch the KB to a different UMLS release
"""
import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import Run, connect, initialize_schema
from src.knowledge_base.validation import kb_summary
from src.umls.rrf_loader import STEPS, KnowledgeBaseBuilder, MissingSourceFileError, ReleaseMismatchError, detect_release
from src.utils.logging_config import setup_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta-dir", default=str(SETTINGS.paths.meta_dir))
    ap.add_argument("--db", default=str(SETTINGS.paths.database))
    ap.add_argument("--release", help="UMLS release label, e.g. 2026AA (auto-detected if omitted)")
    ap.add_argument("--with-attributes", action="store_true")
    ap.add_argument("--force", nargs="*", default=[], choices=STEPS, help="steps to redo")
    ap.add_argument("--replace", action="store_true", help="allow replacing a KB built from another release")
    args = ap.parse_args()
    log = setup_logging("build_knowledge_base", SETTINGS.paths.logs_dir)
    meta = Path(args.meta_dir)
    release = args.release or detect_release(meta)
    if not release:
        log.error("Could not detect the UMLS release from %s. Pass --release 2026AA (for example).", meta)
        sys.exit(2)
    con = connect(args.db)
    initialize_schema(con)
    try:
        with Run(con, "kb_build", umls_release=release, input_file=str(meta)) as run:
            builder = KnowledgeBaseBuilder(con, meta, release, run.run_id, set(args.force), args.with_attributes)
            builder.check_release(args.replace)
            run.stats = builder.build()
    except (MissingSourceFileError, ReleaseMismatchError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    for line in kb_summary(con):
        log.info(line)
    con.close()


if __name__ == "__main__":
    main()
