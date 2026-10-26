"""Stage 5 - consolidate explicit, content-inferred and title-inferred evidence.

One row per (policy, code_system, code) in policy_cpt_mappings; every distinct
piece of supporting evidence is kept in mapping_evidence. Nothing with
evidence is dropped: low-confidence candidates are retained with tier D/E and
needs_review = TRUE. Reviewer decisions are applied per policy only.
"""
from __future__ import annotations

from collections import defaultdict

from config.settings import SETTINGS
from src.mapping.confidence import CORROBORATION_BONUS, tier
from src.mapping.term_matcher import InferredEvidence
from src.policy.code_extractor import CodeMention
from src.utils.ids import stable_id

EXPLICIT_RULE = "explicit-code-2"
# Where an explicitly listed code was found, strongest first.
LOCATION_CLASS = {"title": "explicit", "policy_text": "explicit", "policy_table": "explicit",
                  "policy_qa": "supplementary", "attachment": "supplementary", "code_list_field": "code_list"}
LOCATION_SOURCE = {"title": "policy_title", "policy_text": "policy_content", "policy_table": "policy_table",
                   "policy_qa": "policy_qa", "attachment": "attachment", "code_list_field": "code_list_field"}
LOCATION_PENALTY = {"policy_qa": 5.0, "attachment": 10.0, "code_list_field": 20.0}
PROVENANCE_ORDER = ["title", "policy_table", "policy_text", "policy_qa", "attachment", "code_list_field", "inferred", "reviewer"]


def mapping_id(policy_uid: str, code_system: str, code: str) -> str:
    return stable_id(policy_uid, code_system, code)


def evidence_fingerprint(evidence_rows: list[dict]) -> str:
    return stable_id(*sorted(e["evidence_id"] for e in evidence_rows), length=16)


def consolidate(policy, mentions: list[CodeMention], inferred: list[InferredEvidence], decisions: list[dict],
                code_info: dict, run_id: str, release: str | None) -> tuple[list[dict], list[dict], list[str]]:
    roles = {s.seq: s.section_role for s in policy.sections}
    ev_by_key: dict[tuple, list[dict]] = defaultdict(list)
    status_by_key: dict[tuple, set] = defaultdict(set)

    for m in mentions:
        if not m.mappable:
            continue
        key = (m.code_system, m.code)
        mid = mapping_id(policy.policy_uid, *key)
        info = code_info.get(key, {})
        loc = m.code_location or "policy_text"
        if loc == "policy_text" and m.detection_rule == "table_code_column":
            loc = "policy_table"
        src = LOCATION_SOURCE.get(loc, "policy_content")
        ev_by_key[key].append({
            "evidence_id": stable_id(mid, "explicit_code", src, m.section_seq, m.char_start, m.mention_id),
            "mapping_id": mid, "policy_uid": policy.policy_uid, "evidence_source": src, "method": "explicit_code",
            "section_seq": m.section_seq, "hierarchy_path": _path(policy, m.section_seq),
            "matched_phrase": m.raw_text, "normalized_phrase": m.code, "abbreviation_expanded": None,
            "matched_term": info.get("description"), "matched_term_tty": info.get("tty"), "matched_sab": m.sab,
            "matched_aui": None, "cui": info.get("cui"), "relationship_path": None, "mention_id": m.mention_id,
            "char_start": m.char_start, "char_end": m.char_end, "candidate_set_size": 1,
            "candidate_set_truncated": False, "score": 100.0 - LOCATION_PENALTY.get(loc, 0.0),
            "rule_id": f"{EXPLICIT_RULE}:{m.detection_rule}", "code_location": loc,
            "run_id": run_id, "_srl": info.get("srl"), "_role": roles.get(m.section_seq, "title"),
            "_class": LOCATION_CLASS.get(loc, "explicit"), "_status": m.validation_status, "_qflags": list(m.quality_flags),
        })
        status_by_key[key].add(m.validation_status)

    for e in inferred:
        key = (e.code_system, e.code)
        mid = mapping_id(policy.policy_uid, *key)
        ev_by_key[key].append({
            "evidence_id": stable_id(mid, e.method, e.evidence_source, e.section_seq, e.char_start, e.matched_aui),
            "mapping_id": mid, "policy_uid": policy.policy_uid, "evidence_source": e.evidence_source, "method": e.method,
            "section_seq": e.section_seq, "hierarchy_path": e.hierarchy_path, "matched_phrase": e.matched_phrase,
            "normalized_phrase": e.normalized_phrase, "abbreviation_expanded": e.abbreviation_expanded,
            "matched_term": e.matched_term, "matched_term_tty": e.matched_term_tty, "matched_sab": e.matched_sab,
            "matched_aui": e.matched_aui, "cui": e.cui, "relationship_path": e.relationship_path, "mention_id": None,
            "char_start": e.char_start, "char_end": e.char_end, "candidate_set_size": e.candidate_set_size,
            "candidate_set_truncated": e.candidate_set_truncated, "score": e.score, "rule_id": e.rule_id,
            "code_location": "inferred", "run_id": run_id, "_srl": e.matched_srl, "_role": roles.get(e.section_seq, "title"),
            "_class": "inferred",
        })

    active = {}
    for d in decisions:  # newest active decision wins
        k = (d["code_system"], d["code"])
        if k not in active or d["decided_at"] > active[k]["decided_at"]:
            active[k] = d
    for k, d in active.items():
        if d["decision"] == "add" and k not in ev_by_key:
            mid = mapping_id(policy.policy_uid, *k)
            ev_by_key[k].append({
                "evidence_id": stable_id(mid, "reviewer", d["decision_id"]), "mapping_id": mid,
                "policy_uid": policy.policy_uid, "evidence_source": "reviewer", "method": "reviewer",
                "section_seq": None, "hierarchy_path": None, "matched_phrase": d.get("reason"),
                "normalized_phrase": None, "abbreviation_expanded": None, "matched_term": None,
                "matched_term_tty": None, "matched_sab": None, "matched_aui": None, "cui": None,
                "relationship_path": None, "mention_id": None, "char_start": None, "char_end": None,
                "candidate_set_size": 1, "candidate_set_truncated": False, "score": 100.0,
                "rule_id": f"reviewer:{d['reviewer']}", "code_location": "reviewer", "run_id": run_id, "_srl": None,
                "_role": None, "_class": "reviewer"})

    # policy-level conflict check: inferred title codes vs inferred content codes
    title_codes = {k for k, evs in ev_by_key.items() if any(e["evidence_source"] == "policy_title" and e["method"] != "explicit_code" for e in evs)}
    content_codes = {k for k, evs in ev_by_key.items() if any(e["evidence_source"] == "policy_content" and e["method"] != "explicit_code" for e in evs)}
    explicit_codes = {k for k, evs in ev_by_key.items() if any(e["method"] == "explicit_code" for e in evs)}
    policy_flags = []
    disagreement = bool(title_codes and content_codes and not (title_codes & content_codes))
    if disagreement:
        policy_flags.append("title_content_disagreement")

    mappings, evidence = [], []
    for key, evs in ev_by_key.items():
        system, code = key
        mid = mapping_id(policy.policy_uid, system, code)
        classes = {e["_class"] for e in evs}
        def st(cls):
            return {e["_status"] for e in evs if e["_class"] == cls}
        explicit_st, supp_st, list_st = st("explicit"), st("supplementary"), st("code_list")
        explicit_validated = "validated_in_kb" in explicit_st
        explicit_unvalidated = "no_reference_loaded" in explicit_st and not explicit_validated
        explicit_not_found = bool(explicit_st | supp_st | list_st) and "not_found_in_kb" in (explicit_st | supp_st | list_st) \
            and "validated_in_kb" not in (explicit_st | supp_st | list_st)
        inferred_evs = [e for e in evs if e["_class"] == "inferred"]
        sources = sorted({e["evidence_source"] for e in evs})
        methods = sorted({e["method"] for e in evs})
        best = max(evs, key=lambda e: (e["score"], -PROVENANCE_ORDER.index(e["code_location"]) if e["code_location"] in PROVENANCE_ORDER else -99, e["evidence_id"]))
        score = best["score"]
        inferred_sources = {e["evidence_source"] for e in inferred_evs}
        if {"policy_title", "policy_content"} <= inferred_sources:
            score = min(100.0, score + CORROBORATION_BONUS)
        has_explicit = "explicit" in classes
        has_listed = bool(classes & {"explicit", "supplementary", "code_list"})
        ambiguous = (not has_listed and bool(inferred_evs)
                     and all(e["candidate_set_size"] > 1 for e in inferred_evs))
        conf = tier(score, explicit_validated, explicit_unvalidated, ambiguous)
        flags = []
        if not has_explicit and "supplementary" in classes:
            conf = "B" if "validated_in_kb" in supp_st else ("C" if "no_reference_loaded" in supp_st else conf)
            flags.append("stated_only_in_qa_or_attachment")
        elif not has_explicit and "code_list" in classes:
            conf = "C" if "validated_in_kb" in list_st else ("D" if "no_reference_loaded" in list_st else conf)
            flags.append("listed_only_in_code_list_field")
        for e in evs:
            for qf in e.get("_qflags", []):
                if qf not in flags:
                    flags.append(qf)
        if explicit_not_found:
            flags.append("explicit_code_not_in_kb_release")
            conf = "C" if conf in ("A", "B") else conf
        if explicit_unvalidated:
            flags.append("explicit_code_unvalidated_no_reference")
        if ambiguous:
            flags.append("ambiguous_candidate_set")
        if any(e["candidate_set_truncated"] for e in evs):
            flags.append("candidate_set_truncated")
        if inferred_evs and all(e["method"] == "fuzzy" for e in inferred_evs) and not has_explicit:
            flags.append("fuzzy_only")
        if inferred_evs and all(e["method"] == "related_concept" for e in inferred_evs) and not has_explicit:
            flags.append("related_concept_only")
        if any(e["abbreviation_expanded"] for e in evs):
            flags.append("abbreviation_expansion_used")
        if all(e["_role"] in ("reference", "definition") for e in evs if e["method"] != "reviewer"):
            flags.append("evidence_only_in_reference_or_definition_sections")
        has_conflict = disagreement and key in (title_codes | content_codes) and not has_listed
        if has_conflict:
            flags.append("title_content_disagreement")
        if explicit_codes and not has_listed and inferred_evs:
            flags.append("inferred_code_not_stated_explicitly")

        base = "explicit" if has_explicit else ("supplementary" if "supplementary" in classes else
                                               ("code_list_only" if "code_list" in classes else None))
        if base and inferred_evs:
            origin = f"{base}+inferred"
        elif base:
            origin = base
        elif inferred_evs:
            origin = "inferred"
        else:
            origin = "reviewer_added"
        tiers_present = sorted({e["code_location"] for e in evs}, key=lambda x: PROVENANCE_ORDER.index(x) if x in PROVENANCE_ORDER else 99)

        review_status = "unreviewed"
        d = active.get(key)
        fp = evidence_fingerprint(evs)
        if d:
            review_status = {"approve": "approved", "reject": "rejected", "add": "approved"}[d["decision"]]
            if d.get("evidence_fingerprint") and d["evidence_fingerprint"] != fp and origin != "reviewer_added":
                flags.append("evidence_changed_since_review")
        needs_review = review_status == "unreviewed" and (conf != "A" or ambiguous or has_conflict or explicit_not_found
                                                          or "policy_description_mismatch" in flags)
        title_or_overview = any(e["evidence_source"] == "policy_title" or e["_role"] == "overview" for e in evs)
        info = code_info.get(key, {})
        mappings.append({
            "mapping_id": mid, "policy_uid": policy.policy_uid, "code_system": system, "code": code,
            "code_sab": info.get("sab") or next((e["matched_sab"] for e in evs if e["matched_sab"]), None),
            "mapping_origin": origin, "code_provenance": tiers_present[0], "provenance_tiers": tiers_present,
            "evidence_sources": sources, "methods": methods, "best_method": best["method"],
            "confidence_tier": conf, "score": round(score, 2),
            "cuis": sorted({e["cui"] for e in evs if e["cui"]}), "scope": "policy_level" if title_or_overview else "section_level",
            "section_seqs": sorted({e["section_seq"] for e in evs if e["section_seq"] is not None}),
            "is_ambiguous": ambiguous, "has_conflict": has_conflict, "flags": flags, "needs_review": needs_review,
            "review_status": review_status, "umls_release": release,
            "processing_version": SETTINGS.processing_version, "run_id": run_id,
            "_fingerprint": fp,
        })
        evidence.extend(evs)
    return mappings, evidence, policy_flags


def _path(policy, seq):
    if seq is None:
        return "TITLE"
    for s in policy.sections:
        if s.seq == seq:
            return s.hierarchy_path
    return None
