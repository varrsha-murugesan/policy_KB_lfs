"""Human-in-the-loop review of inferred mappings.

1) Export a review queue (only mappings that need review, highest-impact first):
       python scripts/review_mappings.py export
   -> data/review/review_queue.csv  (fill the 'decision', 'reviewer', 'reason' columns)

2) Import decisions:
       python scripts/review_mappings.py import data/review/review_queue.csv

   decision = approve | reject | add      (blank rows are ignored)
   'add' records a code the reviewer says applies but the system did not find;
   put the code in 'code' and its system (CPT or HCPCS_L2) in 'code_system'.

Decisions are stored in review_decisions and scoped to ONE policy. They are
never turned into global term-to-code rules automatically. A newer decision
for the same policy/code supersedes the older one (kept for audit). Future
mapping runs re-apply active decisions and flag 'evidence_changed_since_review'
when the evidence behind a reviewed mapping has changed.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import Run, connect, current_umls_release, initialize_schema, transaction
from src.mapping.mapping_consolidator import evidence_fingerprint, mapping_id
from src.utils.ids import stable_id
from src.utils.logging_config import setup_logging

QUEUE_COLUMNS = ["policy_uid", "payer", "policy_title", "code_system", "code", "confidence_tier", "score", "best_method",
                 "flags", "evidence_summary", "evidence_fingerprint", "decision", "reviewer", "reason"]


def export_queue(con, out: Path, limit: int, include_all: bool) -> Path:
    where = "TRUE" if include_all else "m.needs_review"
    df = con.execute(f"""
        SELECT m.policy_uid, p.payer, p.policy_title, m.code_system, m.code, m.confidence_tier, m.score, m.best_method,
               array_to_string(m.flags, ';') AS flags, m.mapping_id
        FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
        WHERE {where} AND m.review_status = 'unreviewed'
        ORDER BY m.confidence_tier, m.score DESC LIMIT {int(limit)}""").df()
    summaries, fps = [], []
    for mid in df.mapping_id:
        evs = con.execute("""SELECT evidence_id, evidence_source, method, matched_phrase, hierarchy_path, score
                             FROM mapping_evidence WHERE mapping_id = ? ORDER BY score DESC""", [mid]).fetchall()
        fps.append(evidence_fingerprint([{"evidence_id": e[0]} for e in evs]))
        summaries.append(" || ".join(f"{e[1]}/{e[2]}: '{(e[3] or '')[:60]}' @ {e[4]} ({e[5]})" for e in evs[:5])
                         + (f" || +{len(evs) - 5} more" if len(evs) > 5 else ""))
    df["evidence_summary"], df["evidence_fingerprint"] = summaries, fps
    df["decision"], df["reviewer"], df["reason"] = "", "", ""
    out.parent.mkdir(parents=True, exist_ok=True)
    df[QUEUE_COLUMNS].to_csv(out, index=False, encoding="utf-8")
    return out


def import_decisions(con, path: Path, log) -> int:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = {"policy_uid", "code_system", "code", "decision", "reviewer"} - set(df.columns)
    if missing:
        raise SystemExit(f"Review file is missing columns: {sorted(missing)}")
    df = df[df.decision.str.strip() != ""]
    bad = df[~df.decision.str.strip().str.lower().isin(["approve", "reject", "add"])]
    if len(bad):
        raise SystemExit(f"Invalid decision values in rows {list(bad.index[:10])}; use approve | reject | add")
    if (df.reviewer.str.strip() == "").any():
        raise SystemExit("Every decision needs a reviewer name.")
    known = {r[0] for r in con.execute("SELECT policy_uid FROM policies").fetchall()}
    unknown = set(df.policy_uid) - known
    if unknown:
        raise SystemExit(f"Unknown policy_uid(s): {sorted(unknown)[:5]}")
    release, now, n = current_umls_release(con), datetime.now(), 0
    with transaction(con):
        for r in df.itertuples(index=False):
            decision = r.decision.strip().lower()
            system, code = r.code_system.strip(), r.code.strip().upper()
            con.execute("UPDATE review_decisions SET is_active = FALSE WHERE policy_uid=? AND code_system=? AND code=?",
                        [r.policy_uid, system, code])
            did = stable_id(r.policy_uid, system, code, decision, r.reviewer, now.isoformat())
            con.execute("""INSERT INTO review_decisions (decision_id, policy_uid, code_system, code, decision, reviewer,
                           reason, decided_at, evidence_fingerprint, umls_release, is_active)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)""",
                        [did, r.policy_uid, system, code, decision, r.reviewer.strip(), getattr(r, "reason", None),
                         now, getattr(r, "evidence_fingerprint", None) or None, release])
            status = {"approve": "approved", "reject": "rejected", "add": "approved"}[decision]
            con.execute("UPDATE policy_cpt_mappings SET review_status=?, needs_review=FALSE WHERE mapping_id=?",
                        [status, mapping_id(r.policy_uid, system, code)])
            n += 1
    log.info("Imported %d decisions. 'add' decisions create mappings on the next mapping run.", n)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", default=str(SETTINGS.paths.review_dir / "review_queue.csv"))
    e.add_argument("--limit", type=int, default=500)
    e.add_argument("--all", action="store_true", help="include mappings that do not need review")
    i = sub.add_parser("import")
    i.add_argument("file")
    ap.add_argument("--db", default=str(SETTINGS.paths.database))
    args = ap.parse_args()
    log = setup_logging("review_mappings", SETTINGS.paths.logs_dir)
    con = connect(args.db)
    initialize_schema(con)
    if args.cmd == "export":
        p = export_queue(con, Path(args.out), args.limit, args.all)
        log.info("Review queue written to %s", p)
    else:
        if not Path(args.file).exists():
            log.error("File not found: %s", args.file)
            sys.exit(2)
        with Run(con, "review_import", input_file=args.file) as run:
            run.stats = {"decisions": import_decisions(con, Path(args.file), log)}
    con.close()


if __name__ == "__main__":
    main()
