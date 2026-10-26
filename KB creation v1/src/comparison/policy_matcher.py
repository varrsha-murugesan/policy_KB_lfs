"""Section 9 - payer-agnostic candidate matching between policies of ANY two payers.

Four independent dimensions, each an IDF-weighted cosine similarity in [0, 1]:
  code_similarity        CPT/HCPCS codes of each policy. Weight per code = provenance weight
                         (stated in text 1.0, Q&A/attachment 0.8, code-list field 0.6,
                         inferred tier A-C 0.5) x idf. Cosine (not overlap-over-the-smaller-set)
                         means a 5-code policy inside a 16,000-code list scores ~0.02, so broad
                         code lists cannot dominate.
  concept_similarity     UMLS CUIs recognized in title/content (coded or not), idf-weighted,
                         shrunk by n_shared/(n_shared+1) so one shared concept cannot score 1.0.
  code containment       only when one side is a code_list_policy AND titles agree (>= 0.5):
                         code = max(cosine, 0.6 x share of the smaller policy's codes inside the list).
  relatedness_similarity max of (a) service-family cosine (code_enrichment families) and
                         (b) cosine of CUI sets expanded one hop through loaded UMLS relationships.
                         Relatedness is NEVER treated as equivalence.
  title_similarity       idf-weighted cosine of title words (only for titles judged specific).
idf(x) = ln(1 + N / df(x)) over eligible policies (record_status = ok).

score = w_code*code + w_concept*concept + w_title*title + w_relatedness*relatedness   (0..100)
relation_class = first dimension >= dimension_threshold in the order code, concept, relatedness, title
                 (code_overlap | concept_overlap | clinically_related | title_only | weak_signal)
match_tier:  strong   score >= strong_threshold AND relation_class in (code_overlap, concept_overlap)
                      AND (code or concept >= 0.5) AND no material scope difference AND no code-list policy
             possible score >= possible_threshold (or strong-scored pairs failing a strong condition)
             weak     score >= weak_threshold;  lower: not stored
needs_review is TRUE unless the pair is strong, code-anchored, scope-aligned, with specific titles.
A shared code or concept never implies equivalent coverage or reimbursement rules.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from itertools import combinations

import duckdb

from config.settings import SETTINGS
from src.utils.ids import stable_id
from src.utils.text_normalizer import normalize

log = logging.getLogger(__name__)
TITLE_NOISE = {"policy", "policies", "reimbursement", "payment", "laboratory", "lab", "services", "service", "and", "for",
               "of", "the", "in", "to", "with", "or", "a", "an", "testing", "test", "guidelines", "professional", "facility"}
LOB_TOKENS = {"medicare advantage": "MA", "medicaid": "MCD", "commercial": "COM", "medicare": "MA"}
NEIGHBOR_RELS = ("PAR", "CHD", "RB", "RN", "RQ", "SY")


def title_tokens(title: str | None) -> set[str]:
    return {t for t in normalize(title).split() if t not in TITLE_NOISE and not t.isdigit() and len(t) > 1}


def _lob(lob: str | None) -> set[str]:
    low = (lob or "").lower()
    return {v for k, v in LOB_TOKENS.items() if re.search(rf"\b{k}\b", low)}


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(w * b[k] for k, w in a.items() if k in b)
    if not dot:
        return 0.0
    return round(dot / (math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))), 4)


def load_profiles(con: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    pw = SETTINGS.policy_match.provenance_weight
    prof = {}
    for uid, payer, title, tq, lob, bc, flags, pclass, status in con.execute("""
            SELECT policy_uid, payer, policy_title, COALESCE(title_quality, 'specific'), line_of_business, billing_class,
                   quality_flags, COALESCE(policy_class, 'standard'), COALESCE(record_status, 'ok')
            FROM policies ORDER BY policy_uid""").fetchall():
        if status != "ok":
            continue
        prof[uid] = {"payer": payer, "title": title, "title_quality": tq,
                     "title_tokens": title_tokens(title) if tq == "specific" else set(),
                     "lob": _lob(lob), "lob_raw": lob, "billing_class": bc, "flags": flags or [], "policy_class": pclass,
                     "codes": {}, "explicit": set(), "inferred": set(), "cuis": set(), "families": set(),
                     "neighbors": set(), "settings": set(), "populations": set()}
    for uid, system, code, origin, tier, rs in con.execute("""
            SELECT policy_uid, code_system, code, mapping_origin, confidence_tier, review_status
            FROM policy_cpt_mappings ORDER BY 1, 2, 3""").fetchall():
        p = prof.get(uid)
        if p is None or rs == "rejected":
            continue
        key = f"{system}:{code}"
        if origin.startswith("explicit"):
            cls = "explicit"
        elif origin.startswith("supplementary"):
            cls = "supplementary"
        elif origin.startswith("code_list"):
            cls = "code_list"
        elif tier in ("A", "B", "C") or rs == "approved":
            cls = "inferred"
        else:
            continue  # ambiguous / low-tier inferred candidates do not drive matching
        p["codes"][key] = max(p["codes"].get(key, 0.0), pw[cls])
        (p["explicit"] if cls != "inferred" else p["inferred"]).add(key)
    for uid, cui in con.execute("SELECT policy_uid, cui FROM policy_concepts WHERE method <> 'generic_suppressed'").fetchall():
        if uid in prof:
            prof[uid]["cuis"].add(cui)
    for uid, fam in con.execute("SELECT policy_uid, family_cui FROM policy_service_groups").fetchall():
        if uid in prof:
            prof[uid]["families"].add(fam)
    all_cuis = sorted({c for p in prof.values() for c in p["cuis"]})
    nb = defaultdict(set)
    if all_cuis:
        for c1, c2 in con.execute("""SELECT cui1, cui2 FROM concept_relationships
                                     WHERE cui1 IN (SELECT unnest(?)) AND rel IN (SELECT unnest(?))""",
                                  [all_cuis, list(NEIGHBOR_RELS)]).fetchall():
            nb[c1].add(c2)
    for p in prof.values():
        p["neighbors"] = set().union(*(nb[c] for c in p["cuis"])) if p["cuis"] else set()
    for uid, attrs in con.execute("SELECT policy_uid, attributes FROM policy_rules WHERE NOT is_boilerplate").fetchall():
        if uid in prof:
            a = json.loads(attrs) if attrs else {}
            prof[uid]["settings"].update(a.get("settings", []))
            prof[uid]["populations"].update(x for x in a.get("populations", [])
                                            if x not in ("medicare advantage", "medicaid", "commercial"))
    return prof


def _idf(prof: dict, field: str, keyfn=lambda p, f: p[f]) -> dict:
    n = len(prof)
    df = Counter(k for p in prof.values() for k in keyfn(p, field))
    return {k: math.log(1 + n / d) for k, d in df.items()}


def _vectors(prof):
    idf_code = _idf(prof, "codes", lambda p, f: p[f].keys())
    idf_cui = _idf(prof, "cuis")
    idf_fam = _idf(prof, "families")
    idf_exp = _idf(prof, "x", lambda p, f: p["cuis"] | p["neighbors"])
    idf_tt = _idf(prof, "title_tokens")
    for p in prof.values():
        p["v_code"] = {k: w * idf_code[k] for k, w in p["codes"].items()}
        p["v_cui"] = {k: idf_cui[k] for k in p["cuis"]}
        p["v_fam"] = {k: idf_fam[k] for k in p["families"]}
        p["v_exp"] = {k: idf_exp[k] for k in p["cuis"] | p["neighbors"]}
        p["v_title"] = {k: idf_tt[k] for k in p["title_tokens"]}


def scope_differences(a: dict, b: dict) -> dict:
    diffs = {}
    if a["lob"] and b["lob"] and not (a["lob"] & b["lob"]):
        diffs["line_of_business"] = {"a": a["lob_raw"], "b": b["lob_raw"]}
    if a["billing_class"] and b["billing_class"] and "Both" not in (a["billing_class"], b["billing_class"]) \
            and a["billing_class"] != b["billing_class"]:
        diffs["billing_class"] = {"a": a["billing_class"], "b": b["billing_class"]}
    if a["settings"] and b["settings"] and not (a["settings"] & b["settings"]):
        diffs["care_settings"] = {"a": sorted(a["settings"]), "b": sorted(b["settings"])}
    if a["populations"] and b["populations"] and not (a["populations"] & b["populations"]):
        diffs["populations"] = {"a": sorted(a["populations"]), "b": sorted(b["populations"])}
    return diffs


def _shrink(sim: float, n_shared: int) -> float:
    """Small-sample shrinkage: one shared concept cannot give full similarity."""
    return round(sim * n_shared / (n_shared + 1), 4) if n_shared else 0.0


def _containment(small: dict, large: dict) -> float:
    tot = sum(small.values())
    return round(sum(w for k, w in small.items() if k in large) / tot, 4) if tot else 0.0


def score_pair(a: dict, b: dict) -> dict:
    cfg = SETTINGS.policy_match
    code = _cosine(a["v_code"], b["v_code"])
    concept = _shrink(_cosine(a["v_cui"], b["v_cui"]), len(a["cuis"] & b["cuis"]))
    fam = _shrink(_cosine(a["v_fam"], b["v_fam"]), len(a["families"] & b["families"]))
    exp_shared = len((a["cuis"] | a["neighbors"]) & (b["cuis"] | b["neighbors"]))
    related = max(fam, _shrink(_cosine(a["v_exp"], b["v_exp"]), exp_shared))
    title = _cosine(a["v_title"], b["v_title"])
    containment = 0.0
    code_basis = "cosine"
    if "code_list_policy" in (a["policy_class"], b["policy_class"]) and a["v_code"] and b["v_code"]:
        small, large = (a, b) if len(a["v_code"]) <= len(b["v_code"]) else (b, a)
        if small["policy_class"] != "code_list_policy":
            containment = _containment(small["v_code"], large["v_code"])
            # containment counts only when titles independently agree; discounted so a list never
            # gives full code similarity
            if title >= 0.5 and 0.6 * containment > code:
                code, code_basis = round(0.6 * containment, 4), "containment_with_title_agreement"
    score = round(cfg.w_code * code + cfg.w_concept * concept + cfg.w_title * title + cfg.w_relatedness * related, 2)
    return {"code": code, "concept": concept, "relatedness": related, "title": title, "score": score,
            "containment": containment, "code_basis": code_basis}


def classify(s: dict, a: dict, b: dict, diffs: dict) -> tuple[str, str, bool, list[str]]:
    cfg = SETTINGS.policy_match
    th = cfg.dimension_threshold
    if s["code"] >= th:
        rel = "code_overlap"
    elif s["concept"] >= th:
        rel = "concept_overlap"
    elif s["relatedness"] >= th:
        rel = "clinically_related"
    elif s["title"] >= 0.5:
        rel = "title_only"
    else:
        rel = "weak_signal"
    material = sorted(k for k in diffs if k in ("care_settings", "populations"))
    code_list = "code_list_policy" in (a["policy_class"], b["policy_class"])
    anchored = s["code"] >= 0.5 or s["concept"] >= 0.5
    if s["score"] >= cfg.strong_threshold and rel in ("code_overlap", "concept_overlap") and anchored \
            and not material and not code_list:
        tier = "strong"
    elif s["score"] >= cfg.possible_threshold or s["score"] >= cfg.strong_threshold:
        tier = "possible"
    else:
        tier = "weak"
    reasons = []
    if rel == "title_only":
        reasons.append("title_only_support")
    if rel == "clinically_related":
        reasons.append("relatedness_only_not_equivalence")
    if code_list:
        reasons.append("code_list_policy_involved")
    if diffs:
        reasons.append("scope_differs:" + ",".join(sorted(diffs)))
    shared_codes = set(a["codes"]) & set(b["codes"])
    if shared_codes and not (shared_codes & a["explicit"] & b["explicit"]):
        reasons.append("shared_codes_include_inferred_only")
    if "specific" not in (a["title_quality"],) or "specific" not in (b["title_quality"],):
        reasons.append("non_specific_title")
    if tier != "strong":
        reasons.append(f"tier_{tier}")
    needs_review = not (tier == "strong" and rel == "code_overlap" and not diffs and not code_list
                        and a["title_quality"] == b["title_quality"] == "specific")
    return tier, rel, needs_review, reasons


def match_policies(con: duckdb.DuckDBPyConnection, run_id: str, include_same_payer: bool = False) -> list[dict]:
    cfg = SETTINGS.policy_match
    prof = load_profiles(con)
    _vectors(prof)
    index = defaultdict(set)
    for uid, p in prof.items():
        for k in p["codes"]:
            index[("c", k)].add(uid)
        for k in p["cuis"] | p["families"]:
            index[("u", k)].add(uid)
        for k in p["neighbors"]:
            index[("n", k)].add(uid)
        for k in p["title_tokens"]:
            index[("t", k)].add(uid)
    candidates = set()
    for key in sorted(index):
        uids = index[key]
        if len(uids) > cfg.max_block_df:
            continue
        for x, y in combinations(sorted(uids), 2):
            if include_same_payer or prof[x]["payer"] != prof[y]["payer"]:
                candidates.add((x, y))
    out = []
    for x, y in sorted(candidates):
        a, b = prof[x], prof[y]
        s = score_pair(a, b)
        if s["score"] < cfg.weak_threshold:
            continue
        diffs = scope_differences(a, b)
        tier, rel, review, reasons = classify(s, a, b, diffs)
        shared_explicit = sorted(set(a["codes"]) & set(b["codes"]) & a["explicit"] & b["explicit"])
        shared_other = sorted((set(a["codes"]) & set(b["codes"])) - set(shared_explicit))
        breakdown = {"code_similarity": s["code"], "code_basis": s["code_basis"], "code_containment": s["containment"],
                     "concept_similarity": s["concept"],
                     "relatedness_similarity": s["relatedness"], "title_similarity": s["title"],
                     "weights": {"code": cfg.w_code, "concept": cfg.w_concept, "title": cfg.w_title,
                                 "relatedness": cfg.w_relatedness},
                     "n_codes": [len(a["codes"]), len(b["codes"])], "policy_class": [a["policy_class"], b["policy_class"]]}
        align = "differs" if diffs else ("aligned" if (a["lob"] or a["settings"]) and (b["lob"] or b["settings"]) else "unknown")
        out.append({
            "pair_id": stable_id(x, y), "policy_uid_a": x, "policy_uid_b": y, "payer_a": a["payer"], "payer_b": b["payer"],
            "score": s["score"], "match_tier": tier, "scope_alignment": align, "score_breakdown": json.dumps(breakdown),
            "shared_explicit_codes": shared_explicit[:500], "shared_inferred_codes": shared_other[:500],
            "shared_cuis": sorted(a["cuis"] & b["cuis"])[:200], "scope_differences": json.dumps(diffs),
            "explanation": _explain(s, shared_explicit, shared_other, a, b, diffs, tier, rel), "run_id": run_id,
            "code_similarity": s["code"], "concept_similarity": s["concept"], "relatedness_similarity": s["relatedness"],
            "title_similarity": s["title"], "relation_class": rel, "needs_review": review, "review_reasons": reasons})
    log.info("Scored %d candidate pairs; kept %d (strong=%d, possible=%d, weak=%d)", len(candidates), len(out),
             sum(p["match_tier"] == "strong" for p in out), sum(p["match_tier"] == "possible" for p in out),
             sum(p["match_tier"] == "weak" for p in out))
    return out


def _explain(s, shared_explicit, shared_other, a, b, diffs, tier, rel) -> str:
    parts = [f"code {s['code']:.2f}", f"concept {s['concept']:.2f}", f"related {s['relatedness']:.2f}", f"title {s['title']:.2f}"]
    detail = []
    if shared_explicit:
        detail.append(f"{len(shared_explicit)} shared stated code(s) e.g. {', '.join(shared_explicit[:5])}")
    if shared_other:
        detail.append(f"{len(shared_other)} shared code(s) involving inferred/listed evidence")
    if a["cuis"] & b["cuis"]:
        detail.append(f"{len(a['cuis'] & b['cuis'])} shared UMLS concept(s)")
    text = f"[{tier}/{rel}] " + ", ".join(parts) + ("; " + "; ".join(detail) if detail else "")
    if diffs:
        text += f". Scope differs in: {', '.join(sorted(diffs))} - related, not necessarily equivalent"
    return text + ". Shared codes/concepts do not imply equivalent coverage or reimbursement."
