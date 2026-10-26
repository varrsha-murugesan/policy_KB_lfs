"""Section 10 - deterministic rule comparison and gap analysis for matched pairs.

Kept separate from policy matching so a wrong procedure match cannot silently
become a gap finding: only pairs with tier 'strong' or 'possible' are compared
(weak pairs only with --include-weak), and every finding carries the pair tier.

For each rule type, every non-boilerplate rule of policy A is aligned to its
most similar rule of the same type in policy B:
    similarity = 0.6 * token Jaccard(content words) + 0.4 * attribute overlap
(attribute overlap = Jaccard of codes + modifiers + settings + populations; if
neither rule has attributes, similarity = token Jaccard alone).

Finding types
  equivalent_candidate           aligned, no attribute differences (informational)
  differs_in_limits              aligned, quantities/periods differ (e.g. 20 vs 12 sessions per year)
  differs_in_reimbursement       aligned, percentages differ
  differs_in_modifiers           aligned, modifier sets differ
  differs_in_codes               aligned, code sets differ
  differs_in_indication_scope    aligned, but purpose (screening/diagnostic/...) or population differs:
                                 a scope difference, NOT a rule gap or contradiction (checked first)
  conflict_candidate             aligned, polarities oppose (allow vs deny, require vs not_required)
  not_identified_in_b            no B rule of this type at all, B extraction looks complete
  not_identified_in_b_wording_may_differ  B has rules of this type, none similar enough
  not_assessable_incomplete_extraction    B is flagged incomplete -> no conclusion
  (and the mirrored *_in_a findings)
  codes_only_in_a / codes_only_in_b       from consolidated code mappings
  modifiers_only_in_a / modifiers_only_in_b from explicit modifier mentions

Nothing is declared "missing": absence is reported as "not identified", always
with status needs_review, and suppressed to "not assessable" when extraction
quality flags indicate the absence may be an artifact.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

import duckdb

from config.settings import SETTINGS
from src.policy.policy_metadata import incomplete
from src.utils.ids import stable_id
from src.utils.text_normalizer import BAG_STOPWORDS, GENERIC_WORDS

log = logging.getLogger(__name__)
COMPARED_TYPES = ("prior_authorization", "medical_necessity", "coverage", "exclusion", "frequency_limit",
                  "billing_instruction", "reimbursement", "modifier_requirement", "documentation", "code_applicability")
OPPOSING = {("allow", "deny"), ("deny", "allow"), ("require", "not_required"), ("not_required", "require")}
PAYER_WORDS = {"humana", "unitedhealthcare", "uhc", "aetna", "cigna", "anthem", "ma", "plan", "plans"}


def _content_tokens(norm: str) -> set[str]:
    return {t for t in norm.split() if t not in BAG_STOPWORDS and t not in GENERIC_WORDS and t not in PAYER_WORDS
            and len(t) > 2}


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _attr_set(a: dict) -> set:
    return ({f"c:{x}" for x in a.get("codes", [])} | {f"m:{x}" for x in a.get("modifiers", [])}
            | {f"s:{x}" for x in a.get("settings", [])} | {f"p:{x}" for x in a.get("populations", [])})


def similarity(r1: dict, r2: dict) -> float:
    tj = _jac(r1["tokens"], r2["tokens"])
    a1, a2 = _attr_set(r1["attrs"]), _attr_set(r2["attrs"])
    if not a1 and not a2:
        return round(tj, 3)
    return round(0.6 * tj + 0.4 * _jac(a1, a2), 3)


def load_rules(con, uids) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for rid, uid, rtype, pol, text, norm, attrs in con.execute("""
            SELECT rule_id, policy_uid, rule_type, polarity, rule_text, normalized_text, attributes
            FROM policy_rules WHERE NOT is_boilerplate AND policy_uid IN (SELECT unnest(?))""", [list(uids)]).fetchall():
        a = json.loads(attrs) if attrs else {}
        out[uid].append({"rule_id": rid, "type": rtype, "polarity": pol, "text": text, "attrs": a,
                         "tokens": _content_tokens(norm)})
    return out


def _diff_type(ra: dict, rb: dict) -> tuple[str, dict]:
    a, b = ra["attrs"], rb["attrs"]
    pa, pb = set(a.get("purposes", [])), set(b.get("purposes", []))
    if pa and pb and not (pa & pb):
        return "differs_in_indication_scope", {"purposes": {"a": sorted(pa), "b": sorted(pb)},
                                               "note": "different clinical purpose - not a like-for-like rule difference"}
    sa, sb = set(a.get("populations", [])), set(b.get("populations", []))
    if sa and sb and not (sa & sb):
        return "differs_in_indication_scope", {"populations": {"a": sorted(sa), "b": sorted(sb)}}
    if (ra["polarity"], rb["polarity"]) in OPPOSING:
        return "conflict_candidate", {"polarity_a": ra["polarity"], "polarity_b": rb["polarity"]}
    for key, label in (("quantities", "differs_in_limits"), ("periods", "differs_in_limits"),
                       ("percents", "differs_in_reimbursement"), ("modifiers", "differs_in_modifiers"),
                       ("codes", "differs_in_codes")):
        va, vb = set(a.get(key, [])), set(b.get(key, []))
        if va and vb and va != vb:
            return label, {key: {"a_only": sorted(va - vb), "b_only": sorted(vb - va), "shared": sorted(va & vb)}}
    return "equivalent_candidate", {}


def compare_pair(pair: dict, rules: dict, flags: dict, code_sets: dict, modifier_sets: dict, run_id: str) -> list[dict]:
    a_uid, b_uid = pair["policy_uid_a"], pair["policy_uid_b"]
    findings = []
    thr = SETTINGS.policy_match.min_rule_similarity
    inc = {a_uid: incomplete(flags.get(a_uid)), b_uid: incomplete(flags.get(b_uid))}

    def add(rtype, ftype, ra=None, rb=None, sim=None, detail=None):
        status = "informational" if ftype == "equivalent_candidate" else "needs_review"
        d = dict(detail or {})
        d["pair_tier"] = pair["match_tier"]
        d["scope_alignment"] = pair["scope_alignment"]
        findings.append({"finding_id": stable_id(pair["pair_id"], rtype, ftype, ra and ra["rule_id"], rb and rb["rule_id"],
                                                 json.dumps(detail, sort_keys=True) if detail else None),
                         "pair_id": pair["pair_id"], "rule_type": rtype, "finding_type": ftype,
                         "rule_id_a": ra and ra["rule_id"], "rule_id_b": rb and rb["rule_id"], "similarity": sim,
                         "detail": json.dumps(d, sort_keys=True), "status": status, "run_id": run_id})

    for rtype in COMPARED_TYPES:
        ra_list = [r for r in rules.get(a_uid, []) if r["type"] == rtype]
        rb_list = [r for r in rules.get(b_uid, []) if r["type"] == rtype]
        for side, src, dst, dst_uid, suffix in (("a", ra_list, rb_list, b_uid, "b"), ("b", rb_list, ra_list, a_uid, "a")):
            for r in src:
                best, best_sim = None, 0.0
                for o in dst:
                    s = similarity(r, o)
                    if s > best_sim:
                        best, best_sim = o, s
                if best is not None and best_sim >= thr:
                    if side == "a":  # report aligned pairs once (from A's side)
                        ftype, detail = _diff_type(r, best)
                        add(rtype, ftype, r, best, best_sim, detail)
                    continue
                ra_, rb_ = (r, None) if side == "a" else (None, r)
                if inc[dst_uid]:
                    add(rtype, "not_assessable_incomplete_extraction", ra_, rb_, best_sim or None,
                        {"reason": f"policy {suffix.upper()} extraction flagged incomplete",
                         "flags": flags.get(dst_uid)})
                elif not dst:
                    add(rtype, f"not_identified_in_{suffix}", ra_, rb_, None)
                else:
                    add(rtype, f"not_identified_in_{suffix}_wording_may_differ", ra_, rb_, best_sim or None,
                        {"closest_rule_id": best and best["rule_id"]})

    for label, sets in (("codes", code_sets), ("modifiers", modifier_sets)):
        sa, sb = sets.get(a_uid, set()), sets.get(b_uid, set())
        if sa and sb:  # only compare when both policies state something
            if sa - sb:
                add(f"applicable_{label}", f"{label}_only_in_a", detail={label: sorted(sa - sb), "shared": sorted(sa & sb)})
            if sb - sa:
                add(f"applicable_{label}", f"{label}_only_in_b", detail={label: sorted(sb - sa), "shared": sorted(sa & sb)})
    return findings


def run_gap_analysis(con: duckdb.DuckDBPyConnection, pairs: list[dict], run_id: str, include_weak: bool = False) -> list[dict]:
    pairs = [p for p in pairs if include_weak or p["match_tier"] in ("strong", "possible")]
    uids = {u for p in pairs for u in (p["policy_uid_a"], p["policy_uid_b"])}
    if not uids:
        return []
    rules = load_rules(con, uids)
    flags = dict(con.execute("SELECT policy_uid, quality_flags FROM policies WHERE policy_uid IN (SELECT unnest(?))",
                             [list(uids)]).fetchall())
    code_sets, mod_sets = defaultdict(set), defaultdict(set)
    for uid, system, code in con.execute("""SELECT policy_uid, code_system, code FROM policy_cpt_mappings
            WHERE review_status <> 'rejected' AND (mapping_origin LIKE 'explicit%' OR review_status = 'approved')
              AND policy_uid IN (SELECT unnest(?))""", [list(uids)]).fetchall():
        code_sets[uid].add(f"{system}:{code}")
    for uid, code in con.execute("""SELECT DISTINCT policy_uid, code FROM code_mentions WHERE code_system = 'MODIFIER'
            AND policy_uid IN (SELECT unnest(?))""", [list(uids)]).fetchall():
        mod_sets[uid].add(code)
    findings = []
    for p in pairs:
        findings += compare_pair(p, rules, flags, code_sets, mod_sets, run_id)
    log.info("Gap analysis: %d pairs compared, %d findings", len(pairs), len(findings))
    return findings
