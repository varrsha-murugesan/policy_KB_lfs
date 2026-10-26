"""Map policies to candidate CPT/HCPCS codes using explicit codes, policy content and policy titles.

Usage (any payer; the profile in config/payers/ decides the adapter):
    python scripts/map_policy_cpt_codes.py --input data/input/humana_extracted_contents.json --payer-profile humana
    python scripts/map_policy_cpt_codes.py --input data/input/unitedhealthcare_extracted_contents.json --payer-profile unitedhealthcare
    python scripts/map_policy_cpt_codes.py --input data/input/new_payer.json --payer-profile config/payers/new_payer.json
    python scripts/map_policy_cpt_codes.py --input X.json                    # payer detected from text via profiles
    python scripts/map_policy_cpt_codes.py --fuzzy-content --infer-attachments   # optional, slower

Running the same file again replaces that file's results (idempotent).
Outputs go to data/exports/.
"""
import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import Run, connect, initialize_schema
from src.knowledge_base.validation import MAPPING_CHECKS, run_checks
from src.mapping.pipeline import run_mapping
from src.policy.json_parser import PolicyParseError
from src.reporting.exports import export_mappings, export_reference, quality_dashboard
from src.reporting.audit import write_audit
from src.utils.logging_config import setup_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=str(SETTINGS.paths.input_json))
    ap.add_argument("--payer-profile", help="profile name in config/payers/ or a path to a profile JSON (recommended)")
    ap.add_argument("--payer", help="payer name (used when no profile exists; structure is auto-detected)")
    ap.add_argument("--infer-attachments", action="store_true", help="also run terminology inference on attachment pages")
    ap.add_argument("--db", default=str(SETTINGS.paths.database))
    ap.add_argument("--out", default=str(SETTINGS.paths.exports_dir))
    ap.add_argument("--fuzzy-content", action="store_true", help="enable bounded fuzzy matching on body text too")
    args = ap.parse_args()
    log = setup_logging("map_policy_cpt_codes", SETTINGS.paths.logs_dir)
    inp = Path(args.input)
    if not inp.exists() and args.input == str(SETTINGS.paths.input_json):
        candidates = sorted(SETTINGS.paths.input_json.parent.glob("*.json"))
        if len(candidates) == 1:
            inp = candidates[0]
            log.info("Default input not found; using the only JSON file in data/input: %s", inp.name)
    if not inp.exists():
        log.error("Input not found: %s. Put your extraction JSON in data/input/ or pass --input <path>.", inp)
        sys.exit(2)
    con = connect(args.db)
    initialize_schema(con)
    try:
        with Run(con, "map", input_file=str(inp)) as run:
            run.stats = run_mapping(con, inp, run, payer=args.payer, fuzzy_content=args.fuzzy_content,
                                    profile=args.payer_profile, infer_attachments=args.infer_attachments or None)
    except PolicyParseError as exc:
        log.error("Could not parse %s: %s", inp, exc)
        sys.exit(2)
    out = Path(args.out)
    export_mappings(con, out)
    export_reference(con, out)
    quality_dashboard(con, out)
    write_audit(con, out)
    for name, ok, detail in run_checks(con, MAPPING_CHECKS):
        log.info("check %-40s %s (%s)", name, "PASS" if ok else "FAIL", detail)
    log.info("Run stats: %s", json.dumps({k: v for k, v in run.stats.items()
                                          if k not in ("changed_policy_uids", "generic_phrase_candidates")}))
    con.close()


if __name__ == "__main__":
    main()
