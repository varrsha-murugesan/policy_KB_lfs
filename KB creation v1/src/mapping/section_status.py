"""Per-section mapping status, decided by a fixed precedence (first match wins):

  1 empty_section            no text extracted
  2 explicit_codes           CPT/HCPCS stated in the section text or table
  3 broad_code_range         only unexpanded range endpoints (e.g. '00100-01999' with no reference loaded)
  4 supplementary_codes      codes stated in Q&A or attachment sections
  5 inferred_candidates      UMLS-inferred candidates only (reason 'all_ambiguous' when every one is ambiguous)
  6 unresolved               code-like tokens that failed validation, or procedure-like phrases with no KB match
  7 not_applicable           definition / reference sections, or no procedure content at all
"""
from __future__ import annotations

EXPLICIT_LOC = {"policy_text", "policy_table", "title"}
SUPP_LOC = {"policy_qa", "attachment"}


def section_statuses(policy, mentions, evidence_rows, unmatched, run_id) -> list[dict]:
    by_sec_m, by_sec_e, by_sec_u = {}, {}, {}
    for m in mentions:
        if m.section_seq is not None:
            by_sec_m.setdefault(m.section_seq, []).append(m)
    for e in evidence_rows:
        if e["section_seq"] is not None and e["method"] not in ("explicit_code", "reviewer"):
            by_sec_e.setdefault(e["section_seq"], []).append(e)
    for u in unmatched:
        if u.get("section_seq") is not None:
            by_sec_u.setdefault(u["section_seq"], []).append(u)
    out = []
    for s in policy.sections:
        ms = [m for m in by_sec_m.get(s.seq, []) if m.code_system in ("CPT", "HCPCS_L2")]
        mappable = [m for m in ms if m.mappable]
        reasons = []
        if not (s.content or "").strip():
            status = "empty_section"
        elif any(m.code_location in EXPLICIT_LOC and m.detection_rule != "range_endpoint_not_expanded" for m in mappable):
            status = "explicit_codes"
        elif mappable and all(m.detection_rule == "range_endpoint_not_expanded" for m in mappable):
            status = "broad_code_range"
            reasons = sorted({m.from_range for m in mappable if m.from_range})
        elif any(m.code_location in SUPP_LOC for m in mappable):
            status = "supplementary_codes"
        elif by_sec_e.get(s.seq):
            status = "inferred_candidates"
            if all(e["candidate_set_size"] > 1 for e in by_sec_e[s.seq]):
                reasons = ["all_ambiguous"]
        elif [m for m in ms if not m.mappable] or by_sec_u.get(s.seq):
            status = "unresolved"
            reasons = sorted({f"code:{m.code}:{m.validation_status}" for m in ms if not m.mappable})[:10] + \
                sorted({f"phrase:{u['phrase_norm']}" for u in by_sec_u.get(s.seq, [])})[:10]
        else:
            status = "not_applicable"
            reasons = [f"role:{s.section_role}"]
        out.append({"policy_uid": policy.policy_uid, "section_seq": s.seq, "status": status, "reasons": reasons,
                    "run_id": run_id})
    return out
