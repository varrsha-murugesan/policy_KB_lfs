"""Audit reports (data/exports/audit/): extraction failures, unresolved mappings,
provenance, quality signals, review-required matches. Computed only from the DB."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb

from src.reporting.exports import _write

log = logging.getLogger(__name__)
QUALITY_FLAGS = ["policy_description_mismatch", "inferred_family_differs_from_stated_codes", "non_procedure_semantic_type",
                 "explicit_code_not_in_kb_release", "listed_only_in_code_list_field", "stated_only_in_qa_or_attachment",
                 "title_content_disagreement", "ambiguous_candidate_set", "evidence_changed_since_review"]


def write_audit(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Path]:
    a = out_dir / "audit"
    paths = []
    q = lambda sql, *p: con.execute(sql, list(p)).df()  # noqa: E731
    paths.append(_write(q("""SELECT source_document_id, payer, file_name, file_sha256, adapter, payer_profile, n_records,
                                    schema_report, loaded_run_id, loaded_at FROM source_documents ORDER BY payer, file_name"""),
                        a / "source_documents.csv"))
    paths.append(_write(q("""
        SELECT p.payer, p.policy_uid, p.policy_number, p.policy_title, p.record_status, p.title_quality, p.identifier_quality,
               p.policy_class, p.n_sections, p.n_distinct_codes, array_to_string(p.quality_flags, ';') AS quality_flags,
               p.source_pointer, d.file_name AS source_file
        FROM policies p LEFT JOIN source_documents d USING (source_document_id)
        WHERE p.record_status <> 'ok' OR p.title_quality <> 'specific' OR p.identifier_quality <> 'source_id'
           OR len(p.quality_flags) > 0
        ORDER BY p.payer, p.record_status, p.policy_title"""), a / "extraction_audit.csv"))
    paths.append(_write(q("""
        SELECT p.payer, s.policy_uid, p.policy_title, s.section_seq, ps.hierarchy_path, ps.evidence_location,
               ps.source_pointer, s.status, array_to_string(s.reasons, ';') AS reasons
        FROM section_mapping_status s JOIN policies p USING (policy_uid)
        LEFT JOIN policy_sections ps ON ps.policy_uid = s.policy_uid AND ps.section_seq = s.section_seq
        ORDER BY p.payer, p.policy_title, s.section_seq"""), a / "section_mapping_status.csv"))
    paths.append(_write(q("""
        SELECT 'code_not_validated' AS kind, p.payer, p.policy_title, m.policy_uid, m.section_seq, m.code_system || ':' || m.code AS item,
               m.code_location AS location, m.validation_status AS detail, m.source_pointer
        FROM code_mentions m JOIN policies p USING (policy_uid)
        WHERE m.code_system IN ('CPT', 'HCPCS_L2') AND m.validation_status <> 'validated_in_kb'
        UNION ALL
        SELECT 'range_not_expanded', p.payer, p.policy_title, m.policy_uid, m.section_seq, m.from_range, m.code_location,
               m.validation_status, m.source_pointer
        FROM code_mentions m JOIN policies p USING (policy_uid) WHERE m.detection_rule = 'range_endpoint_not_expanded'
        UNION ALL
        SELECT 'unmatched_procedure_phrase', p.payer, p.policy_title, u.policy_uid, NULL, u.phrase_norm, u.evidence_source,
               'occurrences=' || u.occurrences, u.example_text
        FROM unmatched_terms u JOIN policies p USING (policy_uid)
        UNION ALL
        SELECT 'unclassified_code_token', p.payer, p.policy_title, m.policy_uid, m.section_seq, m.code, m.code_location,
               m.detection_rule, m.source_pointer
        FROM code_mentions m JOIN policies p USING (policy_uid) WHERE m.code_system = 'UNCLASSIFIED'
        ORDER BY 1, 2, 3"""), a / "unresolved_mappings.csv"))
    paths.append(_write(q("""
        SELECT p.payer, m.policy_uid, p.policy_title, p.policy_class,
               COUNT(*) AS mappings,
               COUNT(*) FILTER (WHERE m.mapping_origin LIKE 'explicit%') AS stated_in_text_or_table,
               COUNT(*) FILTER (WHERE m.mapping_origin LIKE 'supplementary%') AS stated_in_qa_or_attachment,
               COUNT(*) FILTER (WHERE m.mapping_origin LIKE 'code_list%') AS code_list_field_only,
               COUNT(*) FILTER (WHERE m.mapping_origin = 'inferred') AS inferred_only,
               COUNT(*) FILTER (WHERE m.mapping_origin LIKE '%+inferred') AS stated_and_inferred,
               COUNT(*) FILTER (WHERE m.is_ambiguous) AS ambiguous,
               COUNT(*) FILTER (WHERE m.needs_review) AS needs_review
        FROM policy_cpt_mappings m JOIN policies p USING (policy_uid) GROUP BY ALL ORDER BY p.payer, mappings DESC"""),
        a / "provenance_summary.csv"))
    paths.append(_write(q("""
        SELECT payer, policy_uid, policy_title, n_distinct_codes, array_to_string(quality_flags, ';') AS quality_flags
        FROM policies WHERE policy_class = 'code_list_policy' ORDER BY n_distinct_codes DESC"""), a / "code_list_policies.csv"))
    flag_rows = q("""
        SELECT p.payer, m.policy_uid, p.policy_title, m.code_system, m.code, m.mapping_origin, m.confidence_tier, f AS signal
        FROM policy_cpt_mappings m JOIN policies p USING (policy_uid), unnest(m.flags) AS t(f)
        WHERE f IN (SELECT unnest(?)) ORDER BY signal, p.payer, p.policy_title, m.code""", QUALITY_FLAGS)
    paths.append(_write(flag_rows, a / "mapping_quality_signals.csv"))
    pairs = q("""
        SELECT m.pair_id, m.payer_a, pa.policy_title AS policy_title_a, m.payer_b, pb.policy_title AS policy_title_b,
               m.match_tier, m.relation_class, m.score, m.code_similarity, m.concept_similarity, m.relatedness_similarity,
               m.title_similarity, array_to_string(m.review_reasons, ';') AS review_reasons, m.explanation
        FROM policy_pair_matches m JOIN policies pa ON pa.policy_uid = m.policy_uid_a
        JOIN policies pb ON pb.policy_uid = m.policy_uid_b WHERE m.needs_review ORDER BY m.score DESC""")
    paths.append(_write(pairs, a / "review_required_matches.csv"))
    last = con.execute("""SELECT stats_json FROM processing_runs WHERE run_type = 'map' AND status = 'succeeded'
                          ORDER BY started_at DESC LIMIT 5""").fetchall()
    cands = sorted({c for (sj,) in last if sj for c in json.loads(sj).get("generic_phrase_candidates", [])})
    import pandas as pd
    paths.append(_write(pd.DataFrame({"phrase_norm": cands}), a / "generic_phrase_candidates.csv"))
    # summary
    lines = ["# Audit summary", ""]
    for payer, n, ok, fail, gen, miss, chash, clp in con.execute("""
            SELECT payer, COUNT(*), COUNT(*) FILTER (WHERE record_status='ok'),
                   COUNT(*) FILTER (WHERE record_status<>'ok'), COUNT(*) FILTER (WHERE title_quality='generic'),
                   COUNT(*) FILTER (WHERE title_quality='missing'), COUNT(*) FILTER (WHERE identifier_quality<>'source_id'),
                   COUNT(*) FILTER (WHERE policy_class='code_list_policy') FROM policies GROUP BY 1 ORDER BY 1""").fetchall():
        lines.append(f"- **{payer}**: {n} records, {ok} ok, {fail} extraction failures, {gen} generic titles, "
                     f"{miss} missing titles, {chash} without a source identifier, {clp} code-list policies")
    lines += ["", "## Section status", "", "| payer | status | sections |", "|---|---|---|"]
    for r in con.execute("""SELECT p.payer, s.status, COUNT(*) FROM section_mapping_status s JOIN policies p USING (policy_uid)
                            GROUP BY 1, 2 ORDER BY 1, 3 DESC""").fetchall():
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} |")
    lines += ["", "## Mapping provenance", "", "| payer | origin | mappings |", "|---|---|---|"]
    for r in con.execute("""SELECT p.payer, m.mapping_origin, COUNT(*) FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
                            GROUP BY 1, 2 ORDER BY 1, 3 DESC""").fetchall():
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} |")
    lines += ["", "## Quality signals", ""]
    for sig, n in flag_rows.groupby("signal").size().items() if len(flag_rows) else []:
        lines.append(f"- {sig}: {n}")
    lines += ["", "## Policy matches", "", "| tier | relation | pairs | needs review |", "|---|---|---|---|"]
    for r in con.execute("""SELECT match_tier, relation_class, COUNT(*), SUM(needs_review::INT) FROM policy_pair_matches
                            GROUP BY 1, 2 ORDER BY 1, 2""").fetchall():
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
    p = a / "audit_summary.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    paths.append(p)
    log.info("Audit reports written to %s", a)
    return paths
