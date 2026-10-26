"""Validate the knowledge base and mapping results.

    python scripts/validate_results.py                     # integrity checks + annotation template
    python scripts/validate_results.py --gold data/review/gold_mappings.csv

Gold file (CSV, UTF-8) - one row per (policy, code) judged by a domain expert:
    source_pdf | policy_uid   (either identifies the policy)
    code                       e.g. 45378 or G0121
    is_applicable              yes | no
    code_system                optional (CPT | HCPCS_L2); inferred from the code shape if blank
Only policies that appear in the gold file are evaluated. For recall to be
meaningful, annotators must ADD rows (is_applicable=yes) for applicable codes
the system did not propose.

Metrics (precision, recall, ambiguity rate) are computed overall, per best
matching method, per confidence tier, and for policies without explicit codes.
Without a gold file, NO accuracy metric is reported; a stratified annotation
template is written instead. Results: data/exports/validation_summary.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import connect, require_schema
from src.knowledge_base.validation import KB_CHECKS, MAPPING_CHECKS, run_checks
from src.reporting.exports import quality_dashboard
from src.utils.logging_config import setup_logging


def _system(code: str) -> str:
    return "HCPCS_L2" if re.fullmatch(r"[A-V]\d{4}", code) else "CPT"


def evaluate(con, gold_path: Path) -> dict:
    g = pd.read_csv(gold_path, dtype=str, keep_default_na=False)
    if "code" not in g or "is_applicable" not in g or not ({"source_pdf", "policy_uid"} & set(g.columns)):
        raise SystemExit("Gold file needs columns: code, is_applicable and source_pdf or policy_uid")
    pol = con.execute("SELECT policy_uid, source_pdf FROM policies").df()
    if "policy_uid" not in g or (g.policy_uid == "").all():
        g = g.merge(pol, on="source_pdf", how="left")
    unresolved = g[g.policy_uid.isna() | (g.policy_uid == "")]
    if len(unresolved):
        print(f"WARNING: {len(unresolved)} gold rows reference unknown policies and are ignored")
        g = g[~g.index.isin(unresolved.index)]
    g["code"] = g.code.str.strip().str.upper()
    g["code_system"] = [cs if cs else _system(c) for cs, c in zip(g.get("code_system", [""] * len(g)), g.code)]
    g["yes"] = g.is_applicable.str.strip().str.lower().isin(["yes", "y", "true", "1"])
    policies = set(g.policy_uid)
    m = con.execute("""SELECT m.policy_uid, m.code_system, m.code, m.best_method, m.confidence_tier, m.mapping_origin,
                              m.is_ambiguous FROM policy_cpt_mappings m WHERE m.policy_uid IN (SELECT unnest(?))""",
                    [list(policies)]).df()
    gold_yes = {(r.policy_uid, r.code_system, r.code) for r in g.itertuples() if r.yes}
    gold_no = {(r.policy_uid, r.code_system, r.code) for r in g.itertuples() if not r.yes}
    m["key"] = list(zip(m.policy_uid, m.code_system, m.code))
    m["correct"] = m.key.isin(gold_yes)
    m["judged"] = m.key.isin(gold_yes | gold_no)
    no_explicit = set(m.groupby("policy_uid").filter(
        lambda x: not x.mapping_origin.str.startswith("explicit").any()).policy_uid) | (policies - set(m.policy_uid))

    def block(df, yes_keys):
        judged = df[df.judged]
        tp = int(judged.correct.sum())
        found = set(df.key) & yes_keys
        return {"proposed": int(len(df)), "judged": int(len(judged)), "true_positive": tp,
                "precision": round(tp / len(judged), 4) if len(judged) else None,
                "recall": round(len(found) / len(yes_keys), 4) if yes_keys else None,
                "ambiguous_rate": round(float(df.is_ambiguous.mean()), 4) if len(df) else None,
                "unjudged_proposals": int(len(df) - len(judged))}

    res = {"gold_file": str(gold_path), "policies_evaluated": len(policies), "gold_applicable_codes": len(gold_yes),
           "overall": block(m, gold_yes),
           "inferred_only": block(m[m.mapping_origin == "inferred"], gold_yes),
           "policies_without_explicit_codes": block(m[m.policy_uid.isin(no_explicit)],
                                                    {k for k in gold_yes if k[0] in no_explicit}),
           "by_method": {k: block(v, gold_yes) | {"recall": None} for k, v in m.groupby("best_method")},
           "by_tier": {k: block(v, gold_yes) | {"recall": None} for k, v in m.groupby("confidence_tier")},
           "note": "Recall is reported only for whole groups (per-method recall is not well defined). "
                   "Scores are ranking aids, not probabilities."}
    return res


def annotation_template(con, out: Path, per_stratum: int = 15) -> Path:
    df = con.execute("""
        SELECT p.source_pdf, p.policy_uid, p.policy_title, m.code_system, m.code, m.confidence_tier, m.best_method,
               m.mapping_origin,
               (SELECT e.matched_phrase FROM mapping_evidence e WHERE e.mapping_id = m.mapping_id ORDER BY e.score DESC LIMIT 1) AS top_phrase
        FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)""").df()
    if df.empty:
        sample = df
    else:
        sample = (df.groupby(["confidence_tier", "best_method"], group_keys=False)
                    .apply(lambda x: x.sample(min(len(x), per_stratum), random_state=42)))
    sample = sample.assign(is_applicable="", annotator="", note="")
    out.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(out, index=False, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(SETTINGS.paths.database))
    ap.add_argument("--gold")
    ap.add_argument("--out", default=str(SETTINGS.paths.exports_dir))
    args = ap.parse_args()
    log = setup_logging("validate_results", SETTINGS.paths.logs_dir)
    con = connect(args.db, read_only=True)
    require_schema(con)
    out = Path(args.out)
    summary = {"integrity": {}}
    failed = 0
    for group, checks in (("knowledge_base", KB_CHECKS), ("mappings", MAPPING_CHECKS)):
        for name, ok, detail in run_checks(con, checks):
            summary["integrity"][f"{group}.{name}"] = {"passed": ok, "detail": detail}
            failed += not ok
            log.info("%-6s %s.%s (%s)", "PASS" if ok else "FAIL", group, name, detail)
    if args.gold:
        summary["evaluation"] = evaluate(con, Path(args.gold))
        log.info("Evaluation: %s", json.dumps(summary["evaluation"]["overall"]))
    else:
        t = annotation_template(con, SETTINGS.paths.review_dir / "gold_annotation_template.csv")
        summary["evaluation"] = None
        summary["evaluation_note"] = (f"No gold file supplied - no accuracy metrics computed. Annotate {t} "
                                      "(fill is_applicable; add rows for missed codes) and re-run with --gold.")
        log.info(summary["evaluation_note"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "validation_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    quality_dashboard(con, out)
    log.info("Wrote validation_summary.json and mapping_quality_report.md to %s", out)
    con.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
