"""Find candidate related policies across payers and compare their rules.

Usage:
    python scripts/compare_policies.py                         # cross-payer pairs only
    python scripts/compare_policies.py --include-same-payer    # also pairs within one payer (overlap analysis)
    python scripts/compare_policies.py --include-weak          # also run gap analysis on weak pairs

Recomputes all pairs and findings each run (idempotent). Outputs in data/exports/:
matched_policy_pairs.csv, policy_comparison_report.csv, rule_gap_report.csv,
unmatched_policies.csv, policy_change_impact.csv
"""
import argparse
from pathlib import Path

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.comparison.policy_matcher import match_policies
from src.comparison.rule_comparator import run_gap_analysis
from src.knowledge_base.db_manager import Run, connect, current_umls_release, initialize_schema, insert_records, transaction
from src.knowledge_base.validation import MAPPING_CHECKS, run_checks
from src.reporting.exports import _write, export_comparisons, quality_dashboard
from src.reporting.audit import write_audit
from src.utils.logging_config import setup_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(SETTINGS.paths.database))
    ap.add_argument("--out", default=str(SETTINGS.paths.exports_dir))
    ap.add_argument("--include-same-payer", action="store_true")
    ap.add_argument("--include-weak", action="store_true")
    args = ap.parse_args()
    log = setup_logging("compare_policies", SETTINGS.paths.logs_dir)
    con = connect(args.db)
    initialize_schema(con)
    n_pol = con.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
    payers = [r[0] for r in con.execute("SELECT DISTINCT payer FROM policies ORDER BY 1").fetchall()]
    if n_pol == 0:
        log.error("No policies in the database. Run scripts/map_policy_cpt_codes.py first.")
        return
    if len(payers) < 2 and not args.include_same_payer:
        log.warning("Only one payer is loaded (%s). Cross-payer matching needs policies from at least two payers - "
                    "map a second payer's file with --payer, or use --include-same-payer for within-payer overlap.", payers)
    with Run(con, "compare", umls_release=current_umls_release(con)) as run:
        pairs = match_policies(con, run.run_id, include_same_payer=args.include_same_payer)
        findings = run_gap_analysis(con, pairs, run.run_id, include_weak=args.include_weak)
        findings = list({f["finding_id"]: f for f in findings}.values())
        with transaction(con):
            con.execute("DELETE FROM rule_gap_findings")
            con.execute("DELETE FROM policy_pair_matches")
            insert_records(con, "policy_pair_matches", pairs)
            insert_records(con, "rule_gap_findings", findings)
        run.stats = {"payers": payers, "pairs": len(pairs), "findings": len(findings),
                     "strong": sum(p["match_tier"] == "strong" for p in pairs),
                     "possible": sum(p["match_tier"] == "possible" for p in pairs),
                     "weak": sum(p["match_tier"] == "weak" for p in pairs)}
    out = Path(args.out)
    export_comparisons(con, out)
    _write(con.execute("""
        SELECT p.payer, p.policy_uid, p.policy_title, array_to_string(p.quality_flags, ';') AS quality_flags,
               (SELECT COUNT(*) FROM policy_cpt_mappings m WHERE m.policy_uid = p.policy_uid) AS n_codes
        FROM policies p WHERE p.policy_uid NOT IN (SELECT policy_uid_a FROM policy_pair_matches
                                                    UNION SELECT policy_uid_b FROM policy_pair_matches)
        ORDER BY p.payer, p.policy_title""").df(), out / "unmatched_policies.csv")
    _write(con.execute("""
        WITH vers AS (SELECT policy_uid, COUNT(*) AS n_versions, MAX(first_seen_at) AS last_change
                      FROM policy_versions GROUP BY 1)
        SELECT m.pair_id, m.match_tier, pa.policy_title AS policy_a, va.n_versions AS versions_a,
               pb.policy_title AS policy_b, vb.n_versions AS versions_b,
               GREATEST(va.last_change, vb.last_change) AS last_content_change,
               (SELECT COUNT(*) FROM rule_gap_findings f WHERE f.pair_id = m.pair_id AND f.status = 'needs_review') AS open_findings
        FROM policy_pair_matches m JOIN policies pa ON pa.policy_uid = m.policy_uid_a
        JOIN policies pb ON pb.policy_uid = m.policy_uid_b JOIN vers va ON va.policy_uid = m.policy_uid_a
        JOIN vers vb ON vb.policy_uid = m.policy_uid_b
        WHERE va.n_versions > 1 OR vb.n_versions > 1
        ORDER BY CASE m.match_tier WHEN 'strong' THEN 0 WHEN 'possible' THEN 1 ELSE 2 END, open_findings DESC""").df(),
        out / "policy_change_impact.csv")
    quality_dashboard(con, out)
    write_audit(con, out)
    for name, ok, detail in run_checks(con, MAPPING_CHECKS):
        log.info("check %-40s %s (%s)", name, "PASS" if ok else "FAIL", detail)
    log.info("Comparison stats: %s", run.stats)
    con.close()


if __name__ == "__main__":
    main()
