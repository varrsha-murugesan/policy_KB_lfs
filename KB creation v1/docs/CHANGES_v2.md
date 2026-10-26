# Version 2 - payer-agnostic pipeline

## Decisions taken (conservative defaults, all configurable)
1. Both delivered formats are supported by adapters; future payers use profiles (declarative adapter).
2. The 50 UHC records with no text and no policy number are kept as `record_status = extraction_failure`,
   their code lists recorded, and they are excluded from inference, rules and matching.
3. Code provenance classes: `explicit` (title / text / table), `supplementary` (Q&A / attachment),
   `code_list_only` (only in an extracted code-list field), `inferred` (UMLS), `reviewer_added`.
   Tiers: explicit validated A; supplementary validated B; code-list-only validated C.
4. Terminology inference runs on titles, body, tables and Q&A. Attachments (mostly code tables) get explicit
   code extraction only; inference and rule extraction there are opt-in (`--infer-attachments`,
   `rules_from_attachments`).
5. Generic-phrase suppression now uses a static versioned list (`config/generic_phrases.json`) instead of
   per-batch statistics, so results never depend on which other policies are in a run.
6. Policies listing >= 150 distinct codes are `code_list_policy`; matching uses IDF-weighted cosine so they
   cannot dominate; containment counts only with independent title agreement.
7. A profile's adapter is overridden by structure detection when it cannot read the file (logged).

## Files
New: `src/ingest/{profiles,base,loader,registry}.py`, `src/ingest/adapters/{sectioned_list,guidelines_document,declarative}.py`,
`src/enrichment/umls_enrichment.py`, `src/mapping/section_status.py`, `src/reporting/audit.py`,
`scripts/profile_umls_kb.py`, `config/payers/{humana,unitedhealthcare}.json`, `config/generic_phrases.json`,
`tests/test_payer_agnostic.py`, `tests/fixtures/payers/acme_health_*`, `docs/ADDING_A_PAYER.md`, `docs/sample_outputs/`.
Modified: `database/schema.sql` (v2, additive migration), `config/settings.py`, `src/policy/{json_parser,code_extractor,rule_extractor}.py`,
`src/mapping/{pipeline,mapping_consolidator,term_matcher,content_mapper,title_mapper,confidence}.py`,
`src/comparison/policy_matcher.py` (rewritten), `src/knowledge_base/db_manager.py`, `src/umls/rrf_loader.py`
(new `code_enrichment` build step), `src/reporting/exports.py`, `scripts/{map_policy_cpt_codes,compare_policies}.py`,
`tests/conftest.py`, `tests/test_policy_comparison.py`.

## Verified (run in the build environment)
- 59 automated tests pass (44 original + 15 new).
- Humana results identical to v1 (470 mappings, 1,717 evidence, 2,121 rules; same policy ids).
- Real Humana and UHC files: results identical when record order is shuffled (mappings, evidence, section
  statuses, rules, policy records) - found and fixed one order-dependent id on the way.
- Clean-copy end-to-end run, three payers, 95 s: 18/18 integrity checks pass.
- An existing v1 database upgrades in place (data kept).

## Not verified / blockers
- No licensed UMLS release in the build environment: all terminology results use the synthetic fixture.
  Run `scripts/profile_umls_kb.py` on your release to see which enrichment capabilities it supports
  (hierarchy families, cross-vocabulary synonyms, relationships).
- Mapping and matching accuracy are unmeasured until an expert-labelled gold set exists
  (`scripts/validate_results.py --gold`).
- Windows execution not tested here (paths use pathlib; no platform-specific code).
