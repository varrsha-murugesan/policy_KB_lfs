-- Open with:  python -c "import duckdb; duckdb.connect('database/payer_policy_kb.duckdb', read_only=True).sql(open('database/example_queries.sql').read().split(';')[0]).show()"
-- or install the DuckDB CLI and run:  duckdb database/payer_policy_kb.duckdb -readonly

-- 1. KB size by code vocabulary
SELECT sab, code_category, COUNT(*) AS n FROM cpt_codes GROUP BY ALL ORDER BY ALL;

-- 2. Every term (all vocabularies) for one CPT code, with its CUI
SELECT t.cui, t.sab, t.tty, t.str, t.suppress FROM v_code_concepts v JOIN terms t ON t.cui = v.cui
WHERE v.code = '45378' ORDER BY t.sab, t.tty;

-- 3. Hierarchy: children of a concept (MRREL semantics: cui2 is CHD of cui1)
SELECT r.cui2, c.preferred_name, r.sab FROM concept_relationships r JOIN concepts c ON c.cui = r.cui2
WHERE r.cui1 = 'C0009378' AND r.rel = 'CHD';

-- 4. Mapping overview per policy: explicit vs inferred
SELECT p.policy_title,
       COUNT(*) FILTER (WHERE m.mapping_origin LIKE 'explicit%') AS explicit_codes,
       COUNT(*) FILTER (WHERE m.mapping_origin = 'inferred') AS inferred_codes,
       COUNT(*) FILTER (WHERE m.needs_review) AS to_review
FROM policies p LEFT JOIN policy_cpt_mappings m USING (policy_uid) GROUP BY 1 ORDER BY 1;

-- 5. Full audit trail for one mapping
SELECT e.evidence_source, e.method, e.hierarchy_path, e.matched_phrase, e.matched_term, e.matched_sab, e.cui,
       e.relationship_path, e.candidate_set_size, e.score, e.rule_id
FROM mapping_evidence e JOIN policy_cpt_mappings m USING (mapping_id) JOIN policies p ON p.policy_uid = m.policy_uid
WHERE p.policy_title = 'Screening Colonoscopy' ORDER BY m.code, e.score DESC;

-- 6. Policies with no explicit codes and what was inferred for them
SELECT p.policy_title, m.code, m.confidence_tier, m.best_method
FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
WHERE m.policy_uid NOT IN (SELECT policy_uid FROM policy_cpt_mappings WHERE mapping_origin LIKE 'explicit%')
ORDER BY 1, 3;

-- 7. Coverage gaps: most frequent procedure-like phrases that matched nothing
SELECT phrase_norm, SUM(occurrences) AS n, COUNT(DISTINCT policy_uid) AS policies
FROM unmatched_terms GROUP BY 1 ORDER BY n DESC LIMIT 50;

-- 8. Policy clusters by shared concepts (17.3)
SELECT c.cui, any_value(k.preferred_name) AS concept, list(DISTINCT p.payer || ': ' || p.policy_title) AS policies
FROM policy_concepts c JOIN policies p USING (policy_uid) JOIN concepts k ON k.cui = c.cui
GROUP BY 1 HAVING COUNT(DISTINCT c.policy_uid) > 1 ORDER BY COUNT(DISTINCT c.policy_uid) DESC;

-- 9. Gap findings needing review for strong pairs
SELECT f.rule_type, f.finding_type, ra.rule_text AS a, rb.rule_text AS b
FROM rule_gap_findings f JOIN policy_pair_matches m USING (pair_id)
LEFT JOIN policy_rules ra ON ra.rule_id = f.rule_id_a LEFT JOIN policy_rules rb ON rb.rule_id = f.rule_id_b
WHERE m.match_tier = 'strong' AND f.status = 'needs_review' ORDER BY 1, 2;

-- 10. Run history (provenance)
SELECT run_id, run_type, status, umls_release, input_sha256, started_at, finished_at FROM processing_runs ORDER BY started_at DESC;
