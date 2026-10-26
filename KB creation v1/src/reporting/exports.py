"""Machine-readable exports.

Licensing: strings from vocabularies with a restriction level above
LicensingSettings.unrestricted_max_srl (CPT is category 3; SNOMED CT is SRL 9)
are replaced by a marker such as "[restricted:CPT srl=3]" unless
PPI_EXPORT_RESTRICTED_STRINGS=true is set. Codes and CUIs are identifiers and
are always exported so results remain traceable to a complete UMLS release.
Policy text is the user's own content and is exported as-is.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import pandas as pd

from config.settings import SETTINGS

log = logging.getLogger(__name__)


def _redact_sql(str_col: str, srl_col: str, sab_col: str) -> str:
    if SETTINGS.licensing.export_restricted_strings:
        return str_col
    lim = SETTINGS.licensing.unrestricted_max_srl
    return (f"CASE WHEN {str_col} IS NULL THEN NULL WHEN COALESCE({srl_col}, 99) <= {lim} THEN {str_col} "
            f"ELSE '[restricted:' || COALESCE({sab_col}, '?') || ' srl=' || COALESCE(CAST({srl_col} AS VARCHAR), '?') || ']' END")


def _write(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    log.info("Wrote %s (%d rows)", path.name, len(df))
    return path


def export_mappings(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Path]:
    desc = _redact_sql("c.description", "v.srl", "c.sab")
    mappings = con.execute(f"""
        SELECT p.policy_uid, p.payer, p.policy_number, p.policy_title, p.source_pdf,
               m.code_system, m.code, m.code_sab, {desc} AS code_description,
               m.mapping_origin,
               CASE WHEN m.review_status = 'approved' THEN 'reviewer_approved'
                    WHEN m.mapping_origin LIKE 'explicit%' THEN 'explicitly_stated'
                    WHEN m.mapping_origin LIKE 'supplementary%' THEN 'stated_in_qa_or_attachment'
                    WHEN m.mapping_origin LIKE 'code_list%' THEN 'code_list_field_only'
                    WHEN m.mapping_origin = 'reviewer_added' THEN 'reviewer_added'
                    ELSE 'inferred' END AS code_status,
               m.code_provenance, array_to_string(m.provenance_tiers, ';') AS provenance_tiers,
               p.record_status, p.policy_class,
               array_to_string(m.evidence_sources, ';') AS evidence_sources,
               array_to_string(m.methods, ';') AS methods, m.best_method, m.confidence_tier, m.score,
               array_to_string(m.cuis, ';') AS cuis, m.scope, array_to_string(m.section_seqs, ';') AS section_seqs,
               m.is_ambiguous, m.has_conflict, array_to_string(m.flags, ';') AS flags, m.needs_review, m.review_status,
               m.umls_release, m.processing_version, m.run_id,
               (SELECT e.matched_phrase FROM mapping_evidence e WHERE e.mapping_id = m.mapping_id ORDER BY e.score DESC LIMIT 1) AS top_matched_phrase
        FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
        LEFT JOIN cpt_codes c ON c.sab = m.code_sab AND c.code = m.code
        LEFT JOIN source_vocabularies v ON v.rsab = c.sab
        ORDER BY p.payer, p.policy_title, m.confidence_tier, m.code""").df()
    paths = [_write(mappings, out_dir / "policy_cpt_mappings.csv")]
    amb = mappings[(mappings.is_ambiguous) | (mappings.has_conflict) | (mappings.needs_review & (mappings.code_status == "inferred"))]
    paths.append(_write(amb, out_dir / "ambiguous_mappings.csv"))

    term = _redact_sql("e.matched_term", "t.srl", "e.matched_sab")
    ev = con.execute(f"""
        SELECT e.mapping_id, e.evidence_id, e.policy_uid, m.code_system, m.code, e.evidence_source, e.method,
               e.section_seq, e.hierarchy_path, e.matched_phrase, e.normalized_phrase, e.abbreviation_expanded,
               {term} AS matched_term, e.matched_term_tty, e.matched_sab, e.matched_aui, e.cui,
               (SELECT preferred_name FROM concepts c WHERE c.cui = e.cui) AS _cui_name,
               (SELECT c2.preferred_sab FROM concepts c2 WHERE c2.cui = e.cui) AS _cui_sab,
               e.relationship_path, e.mention_id, e.code_location, e.semantic_types, e.char_start, e.char_end, e.candidate_set_size,
               e.candidate_set_truncated, e.score, e.rule_id, e.run_id
        FROM mapping_evidence e JOIN policy_cpt_mappings m USING (mapping_id)
        LEFT JOIN terms t ON t.aui = e.matched_aui
        ORDER BY e.policy_uid, m.code, e.score DESC""").df()
    if not SETTINGS.licensing.export_restricted_strings and not ev.empty:
        srl = dict(con.execute("SELECT rsab, srl FROM source_vocabularies").fetchall())
        lim = SETTINGS.licensing.unrestricted_max_srl
        ev["concept_name"] = [n if n is None or (srl.get(s, 99) or 0) <= lim else f"[restricted:{s}]"
                              for n, s in zip(ev._cui_name, ev._cui_sab)]
    else:
        ev["concept_name"] = ev.get("_cui_name")
    ev = ev.drop(columns=["_cui_name", "_cui_sab"], errors="ignore")
    grouped = {}
    for rec in json.loads(ev.to_json(orient="records")):
        grouped.setdefault(rec["mapping_id"], []).append(rec)
    p = out_dir / "mapping_evidence.json"
    p.write_text(json.dumps({"licensing_note": _note(), "evidence_by_mapping": grouped}, indent=1, ensure_ascii=False),
                 encoding="utf-8")
    log.info("Wrote %s (%d mappings)", p.name, len(grouped))
    paths.append(p)

    paths.append(_write(con.execute("""
        SELECT p.payer, p.policy_title, m.policy_uid, m.code_system, m.code, m.raw_text, m.section_seq, m.code_location,
               m.detection_rule, m.validation_status, m.from_range, array_to_string(m.quality_flags, ';') AS quality_flags,
               m.source_pointer, m.context_snippet
        FROM code_mentions m JOIN policies p USING (policy_uid) ORDER BY p.policy_title, m.code_system, m.code""").df(),
        out_dir / "code_mentions.csv"))
    paths.append(_write(con.execute("""
        SELECT p.payer, p.policy_title, u.phrase_norm, u.evidence_source, u.occurrences, u.example_text
        FROM unmatched_terms u JOIN policies p USING (policy_uid) ORDER BY u.occurrences DESC, u.phrase_norm""").df(),
        out_dir / "unmatched_terms.csv"))
    return paths


def export_reference(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Path]:
    """Reference extracts limited to codes/concepts actually used by mappings."""
    desc = _redact_sql("c.description", "v.srl", "c.sab")
    paths = [_write(con.execute(f"""
        SELECT c.sab, c.code, c.code_system, c.code_category, c.primary_cui, {desc} AS description, c.is_suppressed,
               v.srl AS source_restriction_level, c.umls_release
        FROM cpt_codes c LEFT JOIN source_vocabularies v ON v.rsab = c.sab
        WHERE (c.code_system, c.code) IN (SELECT code_system, code FROM policy_cpt_mappings)
        ORDER BY c.sab, c.code""").df(), out_dir / "cpt_reference.csv")]
    term = _redact_sql("t.str", "t.srl", "t.sab")
    paths.append(_write(con.execute(f"""
        SELECT t.cui, t.aui, t.sab, t.code, t.tty, {term} AS term, t.srl, t.suppress, t.is_code_level
        FROM terms t WHERE t.cui IN (SELECT unnest(cuis) FROM policy_cpt_mappings)
        ORDER BY t.cui, t.sab, t.tty""").df(), out_dir / "terminology_terms.csv"))
    paths.append(_write(con.execute("""
        SELECT r.cui1, r.rel, r.rela, r.cui2, r.sab, r.rui FROM concept_relationships r
        WHERE r.cui1 IN (SELECT unnest(cuis) FROM policy_cpt_mappings)
           OR r.cui2 IN (SELECT unnest(cuis) FROM policy_cpt_mappings)
        ORDER BY r.cui1, r.rel""").df(), out_dir / "concept_relationships.csv"))
    return paths


def export_comparisons(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Path]:
    paths = [_write(con.execute("""
        SELECT m.pair_id, m.payer_a, pa.policy_title AS policy_title_a, m.policy_uid_a,
               m.payer_b, pb.policy_title AS policy_title_b, m.policy_uid_b, m.match_tier, m.relation_class, m.score,
               m.code_similarity, m.concept_similarity, m.relatedness_similarity, m.title_similarity, m.needs_review,
               array_to_string(m.review_reasons, ';') AS review_reasons,
               m.scope_alignment, array_to_string(m.shared_explicit_codes, ';') AS shared_explicit_codes,
               array_to_string(m.shared_inferred_codes, ';') AS shared_inferred_codes,
               array_to_string(m.shared_cuis, ';') AS shared_cuis, m.score_breakdown, m.scope_differences,
               m.explanation, m.run_id
        FROM policy_pair_matches m JOIN policies pa ON pa.policy_uid = m.policy_uid_a
        JOIN policies pb ON pb.policy_uid = m.policy_uid_b ORDER BY m.score DESC""").df(),
        out_dir / "matched_policy_pairs.csv")]
    paths.append(_write(con.execute("""
        SELECT f.pair_id, pa.payer AS payer_a, pa.policy_title AS policy_title_a, pb.payer AS payer_b,
               pb.policy_title AS policy_title_b, f.rule_type, f.finding_type, f.status, f.similarity,
               ra.rule_text AS rule_text_a, rb.rule_text AS rule_text_b, f.detail, f.rule_id_a, f.rule_id_b
        FROM rule_gap_findings f JOIN policy_pair_matches m USING (pair_id)
        JOIN policies pa ON pa.policy_uid = m.policy_uid_a JOIN policies pb ON pb.policy_uid = m.policy_uid_b
        LEFT JOIN policy_rules ra ON ra.rule_id = f.rule_id_a LEFT JOIN policy_rules rb ON rb.rule_id = f.rule_id_b
        ORDER BY f.pair_id, f.rule_type, f.finding_type""").df(), out_dir / "rule_gap_report.csv"))
    paths.append(_write(con.execute("""
        SELECT f.pair_id, any_value(m.payer_a) AS payer_a, any_value(pa.policy_title) AS policy_title_a,
               any_value(m.payer_b) AS payer_b, any_value(pb.policy_title) AS policy_title_b,
               any_value(m.match_tier) AS match_tier, any_value(m.scope_alignment) AS scope_alignment, f.rule_type,
               COUNT(*) FILTER (WHERE f.finding_type = 'equivalent_candidate') AS equivalent,
               COUNT(*) FILTER (WHERE f.finding_type LIKE 'differs%') AS differing,
               COUNT(*) FILTER (WHERE f.finding_type = 'conflict_candidate') AS conflicts,
               COUNT(*) FILTER (WHERE f.finding_type LIKE 'not_identified_in_b%' OR f.finding_type LIKE 'only_in_a%') AS only_in_a,
               COUNT(*) FILTER (WHERE f.finding_type LIKE 'not_identified_in_a%' OR f.finding_type LIKE 'only_in_b%') AS only_in_b,
               COUNT(*) FILTER (WHERE f.finding_type LIKE 'not_assessable%') AS not_assessable
        FROM rule_gap_findings f JOIN policy_pair_matches m USING (pair_id)
        JOIN policies pa ON pa.policy_uid = m.policy_uid_a JOIN policies pb ON pb.policy_uid = m.policy_uid_b
        GROUP BY f.pair_id, f.rule_type ORDER BY f.pair_id, f.rule_type""").df(), out_dir / "policy_comparison_report.csv"))
    return paths


def quality_dashboard(con: duckdb.DuckDBPyConnection, out_dir: Path) -> Path:
    """Markdown report computed ONLY from data in the database (17.14)."""
    def q(sql):
        return con.execute(sql).fetchone()[0]

    n_pol = q("SELECT COUNT(*) FROM policies")
    release = q("SELECT COALESCE(MAX(value), 'none') FROM schema_meta WHERE key='active_umls_release'")
    lines = ["# Mapping quality report", "", f"UMLS release in KB: {release}", f"Policies processed: {n_pol}"]
    if n_pol:
        def pct(n):
            return f"{n} ({100.0 * n / n_pol:.1f}%)"

        n_explicit = q("SELECT COUNT(DISTINCT policy_uid) FROM policy_cpt_mappings WHERE mapping_origin LIKE 'explicit%'")
        n_inferred = q("SELECT COUNT(DISTINCT policy_uid) FROM policy_cpt_mappings WHERE mapping_origin LIKE '%inferred%'")
        n_inf_only = q("SELECT COUNT(*) FROM (SELECT policy_uid FROM policy_cpt_mappings GROUP BY 1 "
                       "HAVING bool_and(mapping_origin NOT LIKE 'explicit%') AND bool_or(mapping_origin = 'inferred'))")
        n_noproc = q("SELECT COUNT(*) FROM policies WHERE list_contains(quality_flags, 'no_procedure_evidence')")
        lines += [
            f"Policies with explicit CPT/HCPCS codes: {pct(n_explicit)}",
            f"Policies with inferred codes: {pct(n_inferred)}",
            f"Policies with NO explicit code but inferred codes: {pct(n_inf_only)}",
            f"Policies flagged no_procedure_evidence: {pct(n_noproc)}",
            "", "## Confidence distribution", "", "| tier | mappings | needs review |", "|---|---|---|"]
        for t, n, r in con.execute("SELECT confidence_tier, COUNT(*), SUM(needs_review::INT) FROM policy_cpt_mappings "
                                   "GROUP BY 1 ORDER BY 1").fetchall():
            lines.append(f"| {t} | {n} | {r} |")
        lines += ["", "## Evidence by method", "", "| method | evidence rows |", "|---|---|"]
        for m, n in con.execute("SELECT method, COUNT(*) FROM mapping_evidence GROUP BY 1 ORDER BY 2 DESC").fetchall():
            lines.append(f"| {m} | {n} |")
        lines += ["", f"Ambiguous mappings: {q('SELECT COUNT(*) FROM policy_cpt_mappings WHERE is_ambiguous')}",
                  f"Mappings with title/content conflict: {q('SELECT COUNT(*) FROM policy_cpt_mappings WHERE has_conflict')}",
                  f"Distinct unmatched procedure-like phrases: {q('SELECT COUNT(DISTINCT phrase_norm) FROM unmatched_terms')}",
                  f"Candidate policy pairs: {q('SELECT COUNT(*) FROM policy_pair_matches')}",
                  "", "## Review decisions", ""]
        for d, n in con.execute("SELECT decision, COUNT(*) FROM review_decisions WHERE is_active GROUP BY 1").fetchall():
            lines.append(f"- {d}: {n}")
        lines += ["", "## Runs", "", "| run | type | status | release | started |", "|---|---|---|---|---|"]
        for r in con.execute("SELECT run_id, run_type, status, umls_release, started_at FROM processing_runs ORDER BY started_at DESC LIMIT 15").fetchall():
            lines.append("| " + " | ".join(str(x) for x in r) + " |")
    lines += ["", "_All figures are counts from the database. Precision/recall appear only in validation_summary.json "
              "when a labeled gold file was supplied._"]
    p = out_dir / "mapping_quality_report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _note() -> str:
    return ("Strings from restricted UMLS sources are redacted unless PPI_EXPORT_RESTRICTED_STRINGS=true. "
            "CPT is (c) American Medical Association. Some material in the UMLS Metathesaurus is from copyrighted "
            "sources of the respective copyright holders. Users of the UMLS Metathesaurus are solely responsible for "
            "compliance with any copyright, patent or trademark restrictions and are referred to the copyright, "
            "patent or trademark notices appearing in the original sources, all of which are hereby incorporated "
            "by reference.")
