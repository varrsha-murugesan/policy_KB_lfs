"""Read-only audit of the UMLS resources actually present in the knowledge base.

    python scripts/profile_umls_kb.py

Writes data/exports/umls_profile.json and umls_profile.md with AGGREGATE COUNTS
ONLY (no CPT descriptors or other restricted strings), so the result can be
shared to decide which enrichment capabilities the loaded release supports.
"""
import json
from pathlib import Path

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.knowledge_base.db_manager import connect, require_schema
from src.utils.logging_config import setup_logging

CHECKS = {
    "release": "SELECT value FROM schema_meta WHERE key='active_umls_release'",
    "build_steps": "SELECT step, status, row_count FROM kb_build_steps ORDER BY started_at",
    "vocabularies_loaded": "SELECT sab, COUNT(*) AS atoms, COUNT(DISTINCT cui) AS concepts, MAX(srl) AS srl FROM terms GROUP BY 1 ORDER BY 2 DESC",
    "code_vocab_term_types": "SELECT sab, tty, COUNT(*) FROM terms WHERE is_code_level GROUP BY 1, 2 ORDER BY 1, 3 DESC",
    "codes_by_category": "SELECT sab, code_category, COUNT(*) FROM cpt_codes GROUP BY 1, 2 ORDER BY 1, 2",
    "code_concepts_with_other_vocab_synonyms": """
        SELECT v.sab, COUNT(DISTINCT v.cui) AS code_cuis,
               COUNT(DISTINCT v.cui) FILTER (WHERE EXISTS (SELECT 1 FROM terms t WHERE t.cui = v.cui AND t.sab NOT IN ('CPT','HCPT','HCPCS','MTHCH','MTHHH'))) AS with_other_vocab_atoms
        FROM v_code_concepts v GROUP BY 1""",
    "other_vocabs_sharing_code_concepts": """
        SELECT t.sab, COUNT(DISTINCT t.cui) FROM terms t WHERE t.cui IN (SELECT cui FROM v_code_concepts)
          AND t.sab NOT IN ('CPT','HCPT','HCPCS','MTHCH','MTHHH') GROUP BY 1 ORDER BY 2 DESC LIMIT 25""",
    "relationships_touching_code_concepts": """
        SELECT rel, sab, COUNT(*) FROM concept_relationships
        WHERE cui1 IN (SELECT cui FROM v_code_concepts) OR cui2 IN (SELECT cui FROM v_code_concepts)
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 40""",
    "relationship_labels_touching_code_concepts": """
        SELECT rela, COUNT(*) FROM concept_relationships WHERE rela IS NOT NULL
          AND (cui1 IN (SELECT cui FROM v_code_concepts) OR cui2 IN (SELECT cui FROM v_code_concepts))
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30""",
    "hierarchy_coverage": """SELECT family_source, COUNT(*) AS codes, COUNT(DISTINCT family_cui) AS families
                             FROM code_enrichment GROUP BY 1 ORDER BY 2 DESC""",
    "semantic_types_of_code_concepts": """
        SELECT s.tui, s.sty, COUNT(DISTINCT s.cui) FROM concept_semantic_types s
        WHERE s.cui IN (SELECT cui FROM v_code_concepts) GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 30""",
    "procedure_lexicon_concepts": "SELECT COUNT(*) FILTER (WHERE in_procedure_lexicon), COUNT(*) FILTER (WHERE is_code_bearing), COUNT(*) FROM concepts",
    "definitions_by_source": "SELECT sab, COUNT(*) FROM concept_definitions GROUP BY 1 ORDER BY 2 DESC LIMIT 20",
    "cpt_full_descriptions_loaded": "SELECT COUNT(*) FILTER (WHERE full_description IS NOT NULL), COUNT(*) FROM cpt_codes",
}


def main():
    log = setup_logging("profile_umls_kb", SETTINGS.paths.logs_dir)
    con = connect(SETTINGS.paths.database, read_only=True)
    require_schema(con)
    out = {}
    for name, sql in CHECKS.items():
        try:
            out[name] = [list(r) for r in con.execute(sql).fetchall()]
        except Exception as exc:  # report, never crash the audit
            out[name] = f"unavailable: {type(exc).__name__}: {exc}"
    d = SETTINGS.paths.exports_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "umls_profile.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    md = ["# UMLS knowledge-base profile (aggregate counts only)", ""]
    for name, rows in out.items():
        md.append(f"## {name}")
        md.append("")
        md += [f"- {r}" for r in rows] if isinstance(rows, list) else [f"- {rows}"]
        md.append("")
    (d / "umls_profile.md").write_text("\n".join(md), encoding="utf-8")
    log.info("Wrote umls_profile.json / umls_profile.md to %s", d)


if __name__ == "__main__":
    main()
