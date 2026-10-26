"""Orchestrates Stages 1-5 for a policy file and writes results idempotently.

Re-running with the same input, KB release and code version produces the
same primary keys; derived rows of each processed policy are deleted and
re-inserted inside ONE transaction, so a failure leaves the previous results
intact (Scenario L). Review decisions live in their own table and survive.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

from config.settings import SETTINGS
from src.knowledge_base.db_manager import current_umls_release, insert_records, kb_is_loaded, transaction
from src.mapping.content_mapper import map_content
from src.mapping.mapping_consolidator import consolidate
from src.mapping.term_matcher import TermMatcher
from src.mapping.title_mapper import map_titles
from src.policy.code_extractor import CodeReference, extract_policy_codes
from src.enrichment.umls_enrichment import apply_enrichment, ensure_code_enrichment
from src.ingest.loader import load_policies
from src.mapping.section_status import section_statuses
from src.policy.policy_metadata import quality_flags
from src.policy.rule_extractor import boilerplate_sentences, extract_rules, mark_boilerplate
from src.utils.ids import file_sha256

log = logging.getLogger(__name__)

DERIVED_TABLES = ["mapping_evidence", "policy_cpt_mappings", "code_mentions", "policy_concepts", "unmatched_terms",
                  "policy_rules", "section_mapping_status", "policy_service_groups", "policy_sections"]


def load_generic_phrases() -> tuple[set[str], str]:
    import json
    from src.utils.ids import file_sha256
    from src.utils.text_normalizer import normalize

    f = SETTINGS.matching.generic_phrases_file
    if not f.exists():
        return set(), "none"
    data = json.loads(f.read_text(encoding="utf-8"))
    return {normalize(p) for p in data.get("phrases", [])}, f"{data.get('version', '?')}:{file_sha256(f)[:12]}"


def load_code_info(con) -> dict:
    info = {}
    rows = con.execute("""
        SELECT c.code_system, c.code, c.sab, c.primary_cui, c.description, c.description_tty, v.srl
        FROM cpt_codes c LEFT JOIN source_vocabularies v ON v.rsab = c.sab
        ORDER BY CASE c.sab WHEN 'CPT' THEN 0 WHEN 'HCPCS' THEN 1 ELSE 2 END""").fetchall()
    for system, code, sab, cui, desc, tty, srl in rows:
        info.setdefault((system, code), {"sab": sab, "cui": cui, "description": desc, "tty": tty, "srl": srl})
    return info


def load_decisions(con, policy_uids) -> dict:
    out = defaultdict(list)
    rows = con.execute("""SELECT decision_id, policy_uid, code_system, code, decision, reviewer, reason, decided_at,
                                 evidence_fingerprint FROM review_decisions WHERE is_active AND policy_uid IN (SELECT unnest(?))""",
                       [list(policy_uids)]).fetchall()
    cols = ["decision_id", "policy_uid", "code_system", "code", "decision", "reviewer", "reason", "decided_at",
            "evidence_fingerprint"]
    for r in rows:
        d = dict(zip(cols, r))
        out[d["policy_uid"]].append(d)
    return out


def run_mapping(con: duckdb.DuckDBPyConnection, input_path: Path, run, payer: str | None = None,
                fuzzy_content: bool = False, profile=None, infer_attachments: bool | None = None) -> dict:
    release = current_umls_release(con)
    if not kb_is_loaded(con):
        log.warning("The knowledge base has no CPT/HCPCS codes loaded. Explicit codes will be recorded as "
                    "'no_reference_loaded' and NO terminology inference is possible. Build the KB first.")
    run.umls_release = release
    run.input_sha256 = file_sha256(input_path)
    policies, source_doc = load_policies(input_path, payer=payer, profile=profile)
    source_doc["loaded_run_id"] = run.run_id
    ref = CodeReference.from_db(con)
    code_info = load_code_info(con)
    ensure_code_enrichment(con)
    generic_static, generic_version = load_generic_phrases()
    if infer_attachments is None:
        infer_attachments = SETTINGS.matching.infer_from_attachments

    for p in policies:
        p.quality_flags = p.quality_flags + [f for f in quality_flags(p) if f not in p.quality_flags]
    boilerplate = boilerplate_sentences(policies)
    log.info("Boilerplate sentences per payer: %s", {k: len(v) for k, v in boilerplate.items()})

    mentions = {p.policy_uid: extract_policy_codes(p, ref) for p in policies}
    matcher = TermMatcher(con, fuzzy_sources=("policy_title", "policy_content") if fuzzy_content else ("policy_title",))
    active = [p for p in policies if p.record_status == "ok"]
    content = map_content(matcher, active, boilerplate, include_attachments=infer_attachments)
    titles = map_titles(matcher, active)

    # Generic-phrase suppression uses a STATIC, versioned list (config/generic_phrases.json) so a
    # policy's result never depends on which other policies are in the batch. Corpus-frequent
    # phrases are only REPORTED as candidates for that list.
    phrase_docs = defaultdict(set)
    for e in content.evidence:
        phrase_docs[e.normalized_phrase].add(e.policy_uid)
    n = len(active)
    generic_candidates = sorted(ph for ph, docs in phrase_docs.items()
                                if n >= 10 and len(docs) / n > SETTINGS.matching.generic_phrase_doc_fraction)
    generic = generic_static
    suppressed = [e for e in content.evidence + titles.evidence if e.normalized_phrase in generic]
    if suppressed:
        log.info("Suppressed %d evidence rows for listed generic phrases", len(suppressed))
    content.evidence = [e for e in content.evidence if e.normalized_phrase not in generic]
    titles.evidence = [e for e in titles.evidence if e.normalized_phrase not in generic]

    inferred = defaultdict(list)
    for e in content.evidence + titles.evidence:
        inferred[e.policy_uid].append(e)
    concepts = defaultdict(list)
    for c in content.concepts + titles.concepts:
        concepts[c["policy_uid"]].append(c)
    for e in suppressed:
        concepts[e.policy_uid].append({"policy_uid": e.policy_uid, "cui": e.cui, "evidence_source": e.evidence_source,
                                       "method": "generic_suppressed", "has_code_link": True,
                                       "example_phrase": e.matched_phrase, "section_seq": e.section_seq})
    unmatched = defaultdict(list)
    for u in content.unmatched + titles.unmatched:
        unmatched[u["policy_uid"]].append(u)
    decisions = load_decisions(con, [p.policy_uid for p in policies])

    stats = Counter()
    all_rows = defaultdict(list)
    changed = []
    min_list = SETTINGS.matching.code_list_policy_min_codes
    for p in policies:
        maps, evs, pflags = consolidate(p, mentions[p.policy_uid], inferred[p.policy_uid], decisions[p.policy_uid],
                                        code_info, run.run_id, release)
        p.quality_flags += pflags
        p.n_distinct_codes = len({(m["code_system"], m["code"]) for m in maps if m["mapping_origin"] != "inferred"})
        if p.record_status != "ok":
            p.policy_class = "extraction_failure"
        elif p.n_distinct_codes >= min_list:
            p.policy_class = "code_list_policy"
            p.quality_flags.append(f"large_code_list:{p.n_distinct_codes}")
        elif not maps and not any(c["has_code_link"] for c in concepts[p.policy_uid]):
            p.policy_class = "administrative"
        if p.record_status == "ok" and not maps and not any(c["has_code_link"] for c in concepts[p.policy_uid]):
            p.quality_flags.append("no_procedure_evidence")
        rules = extract_rules(p) if p.record_status == "ok" else []
        mark_boilerplate(rules, boilerplate, p.payer)
        all_rows["section_mapping_status"].extend(
            section_statuses(p, mentions[p.policy_uid], evs, unmatched[p.policy_uid], run.run_id))
        stats[f"policy_class:{p.policy_class}"] += 1
        stats[f"record_status:{p.record_status}"] += 1
        prev = con.execute("SELECT content_sha256 FROM policies WHERE policy_uid=?", [p.policy_uid]).fetchone()
        if prev and prev[0] != p.content_sha256:
            changed.append(p.policy_uid)
        _collect_rows(all_rows, p, mentions[p.policy_uid], maps, evs, concepts[p.policy_uid], unmatched[p.policy_uid],
                      rules, run.run_id)
        stats["policies"] += 1
        stats["mappings"] += len(maps)
        stats["evidence"] += len(evs)
        stats["rules"] += len(rules)
        stats["policies_with_explicit_codes"] += any(m["mapping_origin"].startswith("explicit") for m in maps)
        for m in maps:
            stats[f"origin:{m['mapping_origin']}"] += 1
        stats["policies_with_inferred_codes"] += any("inferred" in m["mapping_origin"] for m in maps)
        stats["mappings_needing_review"] += sum(m["needs_review"] for m in maps)

    uids = [p.policy_uid for p in policies]
    with transaction(con):
        for t in DERIVED_TABLES:
            con.execute(f"DELETE FROM {t} WHERE policy_uid IN (SELECT unnest(?))", [uids])
        con.execute("DELETE FROM source_documents WHERE source_document_id = ?", [source_doc["source_document_id"]])
        insert_records(con, "source_documents", [{**source_doc, "schema_report": _json(source_doc["schema_report"])}])
        for p in policies:
            con.execute("""
                INSERT INTO policies (policy_uid, payer, payer_source, policy_number, policy_title, effective_date,
                    line_of_business, billing_class, source_pdf, content_sha256, n_sections, quality_flags, raw_metadata,
                    last_run_id, updated_at, source_document_id, source_pointer, adapter, record_status, title_quality,
                    identifier_quality, policy_class, n_distinct_codes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (policy_uid) DO UPDATE SET payer=excluded.payer, payer_source=excluded.payer_source,
                    policy_number=excluded.policy_number, policy_title=excluded.policy_title,
                    effective_date=excluded.effective_date, line_of_business=excluded.line_of_business,
                    billing_class=excluded.billing_class, source_pdf=excluded.source_pdf,
                    content_sha256=excluded.content_sha256, n_sections=excluded.n_sections,
                    quality_flags=excluded.quality_flags, raw_metadata=excluded.raw_metadata,
                    last_run_id=excluded.last_run_id, updated_at=now(), source_document_id=excluded.source_document_id,
                    source_pointer=excluded.source_pointer, adapter=excluded.adapter, record_status=excluded.record_status,
                    title_quality=excluded.title_quality, identifier_quality=excluded.identifier_quality,
                    policy_class=excluded.policy_class, n_distinct_codes=excluded.n_distinct_codes""",
                        [p.policy_uid, p.payer, p.payer_source, p.policy_number, p.policy_title, p.effective_date,
                         p.line_of_business, p.billing_class, p.source_pdf, p.content_sha256, len(p.sections),
                         p.quality_flags, _json(p.raw_metadata), run.run_id, p.source_document_id, p.source_pointer,
                         p.adapter, p.record_status, p.title_quality, p.identifier_quality, p.policy_class,
                         p.n_distinct_codes])
            con.execute("INSERT INTO policy_versions (policy_uid, content_sha256, effective_date, first_seen_run) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        [p.policy_uid, p.content_sha256, p.effective_date, run.run_id])
        for table in reversed(DERIVED_TABLES):
            insert_records(con, table, all_rows[table])
        stats.update(apply_enrichment(con, uids, run.run_id))
    stats["changed_policies"] = len(changed)
    stats["generic_phrase_list_version"] = generic_version
    stats["generic_evidence_suppressed"] = len(suppressed)
    stats["generic_phrase_candidates"] = generic_candidates[:100]
    stats["source_document_id"] = source_doc["source_document_id"]
    stats["payer"] = source_doc["payer"]
    stats["unmatched_phrases"] = sum(len(v) for v in unmatched.values())
    result = dict(stats)
    result["changed_policy_uids"] = changed
    return result


def _json(d):
    import json
    return json.dumps(d, ensure_ascii=False, default=str)


def _collect_rows(rows, p, mentions, maps, evs, concepts, unmatched, rules, run_id):
    for s in p.sections:
        rows["policy_sections"].append({
            "policy_uid": p.policy_uid, "section_seq": s.seq, "title": s.title, "section": s.section,
            "subsection": s.subsection, "hierarchy_path": s.hierarchy_path, "page_start": s.page_start,
            "page_end": s.page_end, "content_type": s.content_type, "section_role": s.section_role,
            "scope_label": s.scope_label, "content": s.content, "upstream_codes": s.upstream_codes,
            "table_data": _json(s.table_data) if s.table_data else None, "source_pointer": s.source_pointer,
            "evidence_location": s.evidence_location, "attachment_url": s.attachment_url})
    for m in mentions:
        rows["code_mentions"].append({
            "mention_id": m.mention_id, "policy_uid": m.policy_uid, "section_seq": m.section_seq,
            "code_system": m.code_system, "code": m.code, "raw_text": m.raw_text, "char_start": m.char_start,
            "char_end": m.char_end, "context_snippet": m.context_snippet, "detection_rule": m.detection_rule,
            "validation_status": m.validation_status, "from_range": m.from_range, "run_id": run_id,
            "code_location": m.code_location, "source_pointer": m.source_pointer, "quality_flags": list(m.quality_flags)})
    for m in maps:
        rows["policy_cpt_mappings"].append({k: v for k, v in m.items() if not k.startswith("_")})
    for e in evs:
        rows["mapping_evidence"].append({k: v for k, v in e.items() if not k.startswith("_")})
    agg = {}
    for c in concepts:
        k = (c["cui"], c["evidence_source"])
        a = agg.setdefault(k, {"policy_uid": p.policy_uid, "cui": c["cui"], "evidence_source": c["evidence_source"],
                               "method": c["method"], "has_code_link": False, "example_phrase": c["example_phrase"],
                               "occurrences": 0, "section_seqs": set(), "run_id": run_id})
        a["occurrences"] += 1
        a["has_code_link"] = a["has_code_link"] or c["has_code_link"]
        if c["section_seq"] is not None:
            a["section_seqs"].add(c["section_seq"])
    for a in agg.values():
        a["section_seqs"] = sorted(a["section_seqs"])
        rows["policy_concepts"].append(a)
    ucount = {}
    for u in unmatched:
        k = (u["phrase_norm"], u["evidence_source"])
        if k not in ucount:
            ucount[k] = {**{kk: vv for kk, vv in u.items() if kk != "section_seq"}, "occurrences": 0, "run_id": run_id}
        ucount[k]["occurrences"] += 1
    rows["unmatched_terms"].extend(ucount.values())
    for r in rules:
        rows["policy_rules"].append({
            "rule_id": r.rule_id, "policy_uid": r.policy_uid, "section_seq": r.section_seq,
            "sentence_idx": r.sentence_idx, "rule_type": r.rule_type, "polarity": r.polarity,
            "scope_label": r.scope_label, "rule_text": r.rule_text, "normalized_text": r.normalized_text,
            "attributes": _json(r.attributes), "is_boilerplate": r.is_boilerplate,
            "extraction_rule": r.extraction_rule, "run_id": run_id})
