# Sample outputs (real policy files + SYNTHETIC terminology)

Produced by the full pipeline on the real Humana (114 records) and UnitedHealthcare (147 records)
extraction files plus the synthetic third payer, **against the synthetic UMLS test fixture**
(16 fake codes). Therefore:
- explicit codes from the real policies show `explicit_code_not_in_kb_release` (the fixture lacks real codes);
- inferred mappings, concepts and families come from fixture terminology and are NOT real CPT inferences;
- record statuses, title/identifier quality, provenance classes, code-list detection, section statuses
  and the shape of every report ARE real properties of your files.
Large reports are truncated to their first 200 rows (`*_first200.csv`). Regenerate everything after
loading your licensed UMLS release (README steps 8-12).
