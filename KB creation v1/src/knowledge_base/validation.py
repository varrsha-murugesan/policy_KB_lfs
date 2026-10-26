"""Integrity checks for the knowledge base and the derived mapping tables.
Each check returns (name, passed: bool, detail). Nothing is auto-repaired."""
from __future__ import annotations

import duckdb

KB_CHECKS = [
    ("terms_reference_existing_concepts",
     "SELECT COUNT(*) FROM terms t LEFT JOIN concepts c ON c.cui = t.cui WHERE c.cui IS NULL"),
    ("no_duplicate_code_rows",
     "SELECT COUNT(*) FROM (SELECT sab, code FROM cpt_codes GROUP BY 1, 2 HAVING COUNT(*) > 1)"),
    ("every_code_has_a_code_level_term",
     "SELECT COUNT(*) FROM cpt_codes c WHERE NOT EXISTS (SELECT 1 FROM terms t WHERE t.sab=c.sab AND t.code=c.code AND t.is_code_level)"),
    ("single_release_in_terms",
     "SELECT GREATEST(COUNT(DISTINCT umls_release) - 1, 0) FROM terms"),
    ("no_obsolete_atoms_loaded",
     "SELECT COUNT(*) FROM terms WHERE suppress = 'O'"),
    ("semantic_types_reference_concepts",
     "SELECT COUNT(*) FROM concept_semantic_types s LEFT JOIN concepts c ON c.cui = s.cui WHERE c.cui IS NULL"),
    ("definitions_reference_concepts",
     "SELECT COUNT(*) FROM concept_definitions d LEFT JOIN concepts c ON c.cui = d.cui WHERE c.cui IS NULL"),
    ("codes_reference_concepts",
     "SELECT COUNT(*) FROM cpt_codes k LEFT JOIN concepts c ON c.cui = k.primary_cui WHERE k.primary_cui IS NOT NULL AND c.cui IS NULL"),
    ("relationship_endpoints_exist",
     "SELECT COUNT(*) FROM concept_relationships r WHERE r.cui1 NOT IN (SELECT cui FROM concepts) OR r.cui2 NOT IN (SELECT cui FROM concepts)"),
]

MAPPING_CHECKS = [
    ("every_mapping_has_evidence",
     "SELECT COUNT(*) FROM policy_cpt_mappings m WHERE NOT EXISTS (SELECT 1 FROM mapping_evidence e WHERE e.mapping_id = m.mapping_id)"),
    ("no_duplicate_policy_code_pairs",
     "SELECT COUNT(*) FROM (SELECT policy_uid, code_system, code FROM policy_cpt_mappings GROUP BY 1,2,3 HAVING COUNT(*) > 1)"),
    ("explicit_mappings_have_mentions",
     "SELECT COUNT(*) FROM policy_cpt_mappings m WHERE m.mapping_origin LIKE 'explicit%' AND NOT EXISTS "
     "(SELECT 1 FROM mapping_evidence e WHERE e.mapping_id = m.mapping_id AND e.method = 'explicit_code' AND e.mention_id IS NOT NULL)"),
    ("inferred_evidence_has_phrase_and_term",
     "SELECT COUNT(*) FROM mapping_evidence WHERE method NOT IN ('explicit_code','reviewer') AND (matched_phrase IS NULL OR matched_aui IS NULL)"),
    ("evidence_orphans",
     "SELECT COUNT(*) FROM mapping_evidence e LEFT JOIN policy_cpt_mappings m ON m.mapping_id = e.mapping_id WHERE m.mapping_id IS NULL"),
    ("gap_findings_reference_existing_pairs",
     "SELECT COUNT(*) FROM rule_gap_findings f LEFT JOIN policy_pair_matches m ON m.pair_id = f.pair_id WHERE m.pair_id IS NULL"),
    ("sections_belong_to_policies",
     "SELECT COUNT(*) FROM policy_sections s LEFT JOIN policies p ON p.policy_uid = s.policy_uid WHERE p.policy_uid IS NULL"),
    ("derived_rows_belong_to_policies",
     "SELECT (SELECT COUNT(*) FROM code_mentions x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"
     " + (SELECT COUNT(*) FROM policy_cpt_mappings x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"
     " + (SELECT COUNT(*) FROM policy_rules x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"
     " + (SELECT COUNT(*) FROM policy_concepts x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"
     " + (SELECT COUNT(*) FROM unmatched_terms x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"
     " + (SELECT COUNT(*) FROM policy_versions x WHERE x.policy_uid NOT IN (SELECT policy_uid FROM policies))"),
    ("pairs_reference_policies",
     "SELECT COUNT(*) FROM policy_pair_matches m WHERE m.policy_uid_a NOT IN (SELECT policy_uid FROM policies)"
     " OR m.policy_uid_b NOT IN (SELECT policy_uid FROM policies)"),
]


def run_checks(con: duckdb.DuckDBPyConnection, checks) -> list[tuple[str, bool, str]]:
    out = []
    for name, sql in checks:
        n = con.execute(sql).fetchone()[0]
        out.append((name, n == 0, f"{n} violating rows"))
    return out


def kb_summary(con: duckdb.DuckDBPyConnection) -> list[str]:
    rel = con.execute("SELECT value FROM schema_meta WHERE key='active_umls_release'").fetchone()
    lines = [f"Active UMLS release: {rel[0] if rel else 'none'}"]
    for t in ("concepts", "terms", "concept_semantic_types", "concept_definitions", "cpt_codes", "concept_relationships"):
        lines.append(f"  {t:<24} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>12,}")
    for sab, n in con.execute("SELECT sab, COUNT(*) FROM cpt_codes GROUP BY 1 ORDER BY 1").fetchall():
        lines.append(f"  codes from {sab:<12} {n:>12,}")
    for name, ok, detail in run_checks(con, KB_CHECKS):
        lines.append(f"  check {name:<40} {'PASS' if ok else 'FAIL'} ({detail})")
    return lines
