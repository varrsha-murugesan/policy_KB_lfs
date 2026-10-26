# Design, evaluation and roadmap

## 1. Repository evaluation: `meerapatelmd/metathesaurus`

Inspected by cloning the repository (latest commit 2022-05-17) and reading `DESCRIPTION`, `README.md`, `NEWS.md`, `R/*.R` and `inst/sql/*.sql`.

| Question | Finding (from source) |
|---|---|
| Purpose | R package "Setup UMLS Metathesaurus in Postgres along with optional postprocessing steps" (DESCRIPTION). |
| Extracts / queries / processes? | Loads **already-downloaded** RRF files into Postgres with `COPY` (`copy_rrfs()` in `R/pg-internals.R`). No download, no API client (README points to separate packages for querying and API calls). |
| Vocabularies / identifiers | Generic: creates all MR* tables (`inst/sql/pg.sql`). No vocabulary-specific logic. |
| CPT concepts & descriptions | Only indirectly (whatever MRCONSO contains). The only CPT references are a hard-coded metadata table of source names and restriction levels in `R/mth_docs.R`. |
| Synonyms, semantic types, relationships | Tables are created (MRCONSO, MRSTY, MRREL…) but there is no code that uses them for synonym retrieval or matching. "Crosswalk" (`R/crosswalk.R`) reduces MRCONSO to one string per code per vocabulary. |
| Incremental / reproducible | `run_setup()` drops and recreates the schema (`reset_schema` step); logging table records version/date. No resumable steps. |
| Dependencies | R ≥ 2.10, tidyverse, glue, huxtable, rvest, R.cache, **`pg13` (GitHub-only, same author)**; RxNorm features need the **PL/R Postgres extension**. MySQL support was removed in 4.1.0. |
| DuckDB / Python integration | None. |
| Tests | None in the repository. |

**Decision: build an independent implementation on official UMLS resources.** The repository solves a different problem (bulk-loading the whole Metathesaurus into Postgres from R) and would add an R + Postgres + PL/R stack while providing none of the CPT-specific, matching, provenance or DuckDB functionality required. Two pieces of *knowledge* were reused, not code: the RRF column layouts (they match NLM's documentation) and the need to disable CSV quoting when reading RRF (implemented in `rrf_loader.rrf_relation()` and verified: embedded `"` and `\` survive).

## 2. Data-access approaches compared

| Criterion | UMLS REST API | Downloaded RRF files → DuckDB (chosen) | metathesaurus repo |
|---|---|---|---|
| CPT concepts | yes, if license allows | yes (CPT, HCPT, HCPCS atoms in MRCONSO) | same files, Postgres |
| Preferred terms & synonyms | per-concept calls | all atoms, all term types, set-based | tables only |
| CUIs & semantic types | yes | MRCONSO + MRSTY | tables only |
| Relationships | per-concept, paged | full MRREL, filtered | tables only |
| Cross-vocabulary mappings | via CUI calls | via shared CUI in one join | tables only |
| Completeness | bounded by calls you make; search capped per page | whole release | whole release |
| Speed / scale | 20 requests/s/IP cap; thousands of calls for a KB | one local load; queries in ms | load time similar |
| Auth / rate limits | `apiKey` on every call; caching 12–24 h recommended | key only for the one download | n/a |
| Reproducibility | "current" changes under you | pinned release (`2026AA`) recorded on every row and run | pinned |
| Python + DuckDB | HTTP client | native | R + Postgres |
| Licensing | same license terms | same; keep files private | same |
| Maintenance | simple | moderate (one loader) | R stack |

**API vs. local KB.** The API answers "what is X?" on demand — ideal for verification and occasional lookups, but each policy phrase would need network calls, results depend on the moment you call, and rate limits make bulk matching impractical. A local release-pinned KB lets the matcher join tens of thousands of policy phrases against millions of terms in one query, offline, with identical results on every run. **Hybrid used here:** API for access verification and spot checks; RRF files for everything the pipeline computes.

Other open-source options: QuickUMLS (approximate dictionary matching over MRCONSO) and scispaCy's UMLS linker were considered conceptually but **not inspected in this work**; neither handles CPT licensing, explicit code extraction, provenance tables or payer-policy comparison, and the latter relies on statistical NER models. They are not dependencies.

## 3. API key, access and licensing

- Every UMLS API request needs `apiKey=<key>`; the Download API also takes it (https://documentation.uts.nlm.nih.gov). NLM asks for ≤ 20 requests/second per IP and recommends caching 12–24 h — the client uses 8 req/s, retries with back-off on 429/5xx, and caches responses without the key.
- A key authenticates you; it does **not** by itself grant every vocabulary. In the 2026AA license appendix **CPT is Category 3** (AMA), **HCPT** (CPT inside HCPCS) is Category 3, **HCPCS Level II** carries no additional category, and **SNOMED CT** is governed by Appendix 2. Category 3 limits use to internal research, product development and statistical analysis at the licensee's site(s) and excludes, among others, public systems and copying except for backup/archival (license §12.3). The AMA CPT notice must be displayed.
- Consequences built into the code: RRF files, the DuckDB file and exports are git-ignored; exported strings from sources with restriction level > 0 are redacted unless `PPI_EXPORT_RESTRICTED_STRINGS=true`; codes and CUIs (identifiers) are always kept so results stay traceable (license §11c). If you build a product for others, or production claim-processing use, confirm with AMA/legal whether your CPT rights cover it — the UMLS license alone may not.
- The license requires an annual usage report to NLM (§5).

## 4. Input file: actual structure (`extraction_rules.json`)

List of 114 policies: `policy_title, policy_number, effective_date, source_pdf, line_of_business, billing_class, extracted_content[]`; each element: `title, section, subsection, hierarchy_path, page_number/page_start/page_end, content, codes[], table_data{columns,row}|null, content_type ∈ {paragraph (315), bullet_point (107), definition (54), table_row (909)}`.

Findings that shaped the parser and pipeline:
- **No payer field.** 71 policies name Humana; 43 name no payer → `--payer` CLI argument, else text detection, else file majority; `payer_source` records which.
- **`policy_number` is not unique** (`CP2013101`, `LAB2023027` each label two policies) → key = payer + hash(`source_pdf`).
- **Upstream `codes` list is unreliable**: mixes CPT, HCPCS, `Modifier-XX`, `RevCode-NNNN`, ICD-10, DRG, CPT MAAA (`0012M`) and false positives (`X12` = ANSI standard, `J01` antigen name, `B12` vitamin), and misses codes in text (modifiers PT, 33 in *Screening Colonoscopy*) → treated as hints; everything is re-extracted and re-validated.
- Code ranges appear (`98940 – 98942`, `00100 – 01999`, `G0380 – G0384`) → expanded only against reference data.
- 29 policies contain no codes; many are administrative (claim disputes, fees) and correctly end with `no_procedure_evidence`.
- Some paragraphs are truncated mid-sentence ("...mileage is not") → `possible_truncated_paragraphs` flag (23 policies) that gap analysis respects.
- Section names carry scope: "Kentucky Medicaid Payment Policy" → `scope_label = Kentucky Medicaid`; "Definitions of Italicized Terms" and LCD reference sections get lower evidence weight.

Tolerated variants: root object wrapping a list (`policies`, `data`, `items`, `results`, `documents`), single policy object, JSON Lines, aliases (`title/policy_name`, `policy_id`, `sections/contents`, `text/body`).

## 5. Architecture

```
UMLS RRF ──rrf_loader──► terminology KB (DuckDB) ◄── term_matcher (SQL joins)
                                     ▲                        ▲
policy JSON ─json_parser─► policies/sections ─code_extractor─┤ explicit codes
                                     │        ─content_mapper─┤ content evidence
                                     │        ─title_mapper──┘ title evidence
                                     ▼
                       mapping_consolidator ─► policy_cpt_mappings + mapping_evidence
                       rule_extractor ──────► policy_rules
                                     ▼
                 policy_matcher ─► policy_pair_matches ─► rule_comparator ─► rule_gap_findings
```

### Tables and relationships
Terminology: `source_vocabularies` (versions, restriction levels) · `concepts` (CUI, preferred name, code-bearing / procedure-lexicon flags) · `concept_semantic_types` · `concept_definitions` · `terms` (one row per atom/AUI: original string + normalized string + bag key + descriptor head, SAB, code, TTY, suppression, SRL) · `cpt_codes` (CPT/HCPT/HCPCS codes with category and source description; a code links to several CUIs through `terms`, view `v_code_concepts`) · `concept_relationships` (MRREL subset). `kb_build_steps` makes the build resumable.

Policies: `policies` (uid, payer + provenance, metadata, content hash, quality flags) · `policy_versions` (content hash history) · `policy_sections` (original text, roles, scope) · `code_mentions` (every code-like token with location, detection rule, validation status) · `policy_concepts` (UMLS concepts recognized, coded or not) · `unmatched_terms` · `policy_rules`.

Mappings: `policy_cpt_mappings` (one row per policy × code) → `mapping_evidence` (all evidence) · `review_decisions` (policy-scoped human decisions, superseded not deleted).

Comparison: `policy_pair_matches` → `rule_gap_findings`. Provenance: `processing_runs` (every run with settings snapshot, input SHA-256, UMLS release, stats, errors), `schema_meta`.

Keys are deterministic hashes (`stable_id`), so re-runs reproduce the same IDs. **Foreign keys are logical, not declared**: DuckDB checks FKs against the state at the start of a transaction, so the atomic "delete derived rows → update policy → insert" re-run raises constraint errors when FKs are declared (reproduced during development with DuckDB 1.5). Every logical FK and uniqueness rule is covered by the 18 integrity checks in `validation.py`, run after every build and mapping run. Indexes exist on the lookup columns (`terms.str_norm/str_key/head_norm/code/cui`, relationship endpoints, evidence by mapping).

### KB scope
Atoms of CPT, HCPT, HCPCS and hierarchy sources MTHCH/MTHHH (all), plus English atoms of every concept that is code-bearing **or** has a procedure semantic type (T058–T061, T063), limited by `PPI_MAX_LEXICON_SRL`. Obsolete atoms (`SUPPRESS=O`) excluded; suppressible atoms kept with a score penalty.

## 6. Matching logic and confidence

Windows of 1–8 tokens per sentence (stopword edges, generic-only phrases and short single tokens dropped; policy-local abbreviations expanded only when defined as "long form (ABBR)" and the initials check passes). They are joined to the KB:

| Method | Rule | Base score |
|---|---|---|
| explicit_code | code token in a code context / code table column / upstream list, validated | 100 |
| exact_preferred_term | phrase = code's PT | 85 |
| exact_synonym | phrase = another code-level term of that code (SY, ETCLIN…) | 80 |
| cross_vocab_synonym | phrase = term from another vocabulary on the **same CUI** as the code | 72 |
| normalized_phrase | same words, different order/plural | 68 |
| descriptor_head | phrase = leading clause of a code descriptor ("colonoscopy") | 58 |
| hierarchy_group | phrase = CPT/HCPCS group heading → descendants | 45 |
| fuzzy | rapidfuzz token_sort_ratio ≥ 92 (titles by default) × ratio | 40 |
| related_concept | via RQ, SY, RN or CHD relationship | 35 |

Penalties: candidate set > 1 → −min(25, 6·log2 n); reference section −10, definition −5; suppressible term −5; single token capped at 50. Mapping score = best evidence + 5 if title and content agree. Tiers: **A** explicit & validated; **B** explicit without reference, or ≥75 & unambiguous; **C** ≥55; **D** ≥35; **E** below. Longest match wins within a sentence (nested "colonoscopy" is subsumed by "screening colonoscopy"). Scores are ordinal ranking aids, **not** probabilities; thresholds must be calibrated with `validate_results.py --gold`.

**Relationship policy (17.4).** Used to generate candidates: SY, RQ (possibly synonymous), RN/CHD (narrower — the more general phrase points to more specific codes, always flagged). Stored but **not** used: RB/PAR (broader codes would over-claim), RO incl. `add_on_code_for` (co-billing, not equivalence). Configurable in `VocabularyScope.candidate_rels`.

**Policy matching** and **gap finding types** are documented at the top of `src/comparison/policy_matcher.py` and `src/comparison/rule_comparator.py`. Key safeguards: gap analysis only for strong/possible pairs; boilerplate sentences (repeated in ≥ 20 % of a payer's policies) excluded; indication/population differences reported as `differs_in_indication_scope` before any conflict; absence reported as `not_identified_in_*` (needs review) or `not_assessable_incomplete_extraction`, never "missing".

## 7. Known limitations
- Accuracy is unmeasured until run on the licensed release with a labelled sample.
- CPT descriptors are long; many policy phrases hit only descriptor heads → ambiguous candidate sets (by design: preserved and flagged, not guessed).
- Rule extraction is pattern-based: it catches explicit cues (covered / not covered / up to N per year / prior authorization / modifier …) and will miss rules phrased unusually; polarity in subordinate clauses can be wrong. All findings are review items.
- Payer detection from text is heuristic → pass `--payer`.
- Fuzzy matching on body text is off by default (`--fuzzy-content`) for precision and speed.
- Reviewer decisions are policy-scoped; promoting a reviewed phrase→code pair to a global alias is deliberately not automated.

## 8. Development phases

| Phase | Files | Commands | Done when |
|---|---|---|---|
| 1 Evaluation | docs/DESIGN.md | `verify_umls_access.py` | key works, CPT visible, decision recorded |
| 2 KB | src/umls/*, schema.sql | `download_umls_data.py`, `build_knowledge_base.py` | all KB checks PASS; code counts per SAB plausible |
| 3 Parsing + explicit codes | json_parser, code_extractor | `map_policy_cpt_codes.py` | `code_mentions.csv` reviewed; no bare numbers mapped |
| 4 Content/title inference | term_matcher, *_mapper, consolidator | same | every mapping has evidence; title and content both contribute |
| 5 Validation | confidence, review_mappings, validate_results | `validate_results.py --gold`, review import | precision/recall per tier known; tier thresholds adjusted |
| 6 Policy matching | policy_matcher | `compare_policies.py` | strong pairs spot-checked by experts |
| 7 Gap analysis | rule_extractor, rule_comparator | `compare_policies.py` | findings sampled; false-gap rate known |
| 8 Advanced | see roadmap | — | per feature |

## 9. Roadmap (Section 17), prioritized

| # | Feature | Status in this code | Value | Needs | Complexity | Main risk | Phase |
|---|---|---|---|---|---|---|---|
| 17.8 | Mapping explainability | **implemented** (`mapping_evidence`, flags, rule ids) | high (audit) | — | done | — | now |
| 17.9 | Human review loop | **implemented** (policy-scoped) | high | reviewers | done | reviewer inconsistency → track agreement | now |
| 17.7 | Terminology coverage gaps | **implemented** (`unmatched_terms`, `policy_concepts` without code link) | high | reviewer triage | done | noisy phrases | now |
| 17.14 | Quality dashboard | **implemented** (markdown from DB counts) | medium | gold data for accuracy | low | — | now |
| 17.1 | Concept-based matching | **implemented** (shared CUIs in pair score, recognized-but-uncoded concepts) | high | full KB | done | broad concepts link too much → keep weight 15 | 6 |
| 17.2/17.13 | Gap & consistency analysis | **foundation implemented** | very high | labelled findings | high | wording vs intent | 7 |
| 17.5 | Scope & intent | **partial** (LOB, billing class, settings, populations, purposes) | high | more attribute patterns (age, diagnosis codes, POS) | medium | false "differs" | 7 |
| 17.6/17.11 | Version & change impact | **partial** (`policy_versions`, `policy_change_impact.csv`) | high | versioned inputs over time; KB release diff | medium | — | 8 |
| 17.3 | Clustering | **query provided** (example_queries.sql #8) | medium | — | low–medium (connected components on shared CUIs/codes) | giant clusters via generic concepts | 8 |
| 17.10 | More vocabularies | configurable (`PPI_MAX_LEXICON_SRL`) | medium | license review per source; ICD-10-PCS is unrestricted | low | license | 8 |
| 17.4 | Relationship tuning | configurable (`candidate_rels`) | medium | gold data per relation type | low | over-generation | 5–8 |
| 17.12 | Knowledge graph | **not recommended yet** | low–medium | — | — | ops cost | later |

**Graph (17.12).** The model is already a graph in relational form (`concepts`, `terms`, `concept_relationships`, `v_code_concepts`, `policy_concepts`, `policy_cpt_mappings`); DuckDB recursive CTEs handle the bounded traversals needed (hierarchy depth ≤ 4). A separate graph database adds operations and synchronization cost without a current query it uniquely enables. Revisit if multi-hop exploration UIs become a product requirement (DuckDB's community graph extension or an exported network for analysis would be the lighter next step).
