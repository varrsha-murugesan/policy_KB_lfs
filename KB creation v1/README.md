# Payer Policy Intelligence — UMLS-powered CPT knowledge base (Python + DuckDB)

Deterministic pipeline (no LLMs, no generative AI) that:

1. builds a local, release-pinned **terminology knowledge base** in DuckDB from the UMLS Metathesaurus (CPT, HCPCS, and synonyms from other vocabularies on the same concepts);
2. finds **explicit** CPT/HCPCS codes in payer policies and **infers candidate codes** from policy **content** and, independently, from policy **titles**, with full evidence for every candidate;
3. identifies **related policies across payers** with an explainable score and scope check;
4. compares their **rules** and reports differences and possibly missing rules — saying "not assessable" when the extraction looks incomplete.

Design rationale, the repository evaluation, licensing analysis, schema and roadmap are in **[docs/DESIGN.md](docs/DESIGN.md)**.

> **Status, honestly stated.** All code runs and 44 automated tests pass against a *synthetic* UMLS fixture and synthetic policies. The full pipeline has also run on your real 114-policy Humana file — but against the synthetic knowledge base, because licensed UMLS files cannot be obtained in the build environment. Mapping **accuracy has not been measured**: that requires the real UMLS release (Step 8) and a small expert-labelled sample (Step 11).

---

## Version 2: any payer
Each payer's `extracted_contents.json` is read through a **payer profile** (`config/payers/*.json`) and an
adapter into a common model; the mapping, UMLS enrichment and matching engine is shared. Profiles exist for
Humana and UnitedHealthcare; adding a payer is a configuration task: see **[docs/ADDING_A_PAYER.md](docs/ADDING_A_PAYER.md)**.
What changed and why: **[docs/CHANGES_v2.md](docs/CHANGES_v2.md)**. Example outputs: **[docs/sample_outputs/](docs/sample_outputs/)**.

```powershell
python scripts\initialize_database.py
python scripts\build_knowledge_base.py                      # after Step 8 (UMLS files)
python scripts\profile_umls_kb.py                           # what your UMLS release actually supports
python scripts\map_policy_cpt_codes.py --input data\input\humana_extracted_contents.json --payer-profile humana
python scripts\map_policy_cpt_codes.py --input data\input\unitedhealthcare_extracted_contents.json --payer-profile unitedhealthcare
python scripts\compare_policies.py                          # all payer pairs
python scripts\validate_results.py
python -m pytest
```
Audit reports: `data\exports\audit\` (extraction failures, unresolved mappings, provenance, code-list policies,
quality signals, review-required matches, section status, source-document schema reports).

## Step-by-step on Windows with VS Code

All commands are for **PowerShell**, run from the project folder (`payer_policy_intelligence`).

### Step 1 — Prerequisites
- **Python 3.11 or 3.12 (64-bit)** from https://www.python.org/downloads/ — during installation tick **"Add python.exe to PATH"**. Check: `py --version`
- **VS Code** with the **Python** extension (Microsoft).
- An **active UMLS license** and **API key** (UTS → *My Profile*).
- **Disk space**: the Metathesaurus zip is several GB; the script extracts only the needed tables, but MRREL alone is large. Keep plenty of free space (tens of GB if you also extract MRSAT).

### Step 2 — Create the project
Unzip `payer_policy_intelligence.zip`, e.g. into `C:\work\payer_policy_intelligence`, then:
```powershell
cd C:\work\payer_policy_intelligence
code .
```
Put your extraction JSON in `data\input\`. The default name is `extraction_contents.json`; if that file is missing and `data\input\` contains exactly one `.json` file, that file is used (your uploaded `extraction_rules.json` is already there).

### Step 3 — Virtual environment
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```
If PowerShell blocks the script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (answer **Y**), then activate again. The prompt now starts with `(.venv)`. In VS Code: *Ctrl+Shift+P → Python: Select Interpreter →* `.venv`.

### Step 4 — Install dependencies
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```
`requirements.txt`: duckdb, pandas, requests, python-dotenv, rapidfuzz, pytest. Nothing else.

### Step 5 — Configure the UMLS key (never in code)
```powershell
Copy-Item .env.example .env
notepad .env
```
Replace `paste-your-key-here` with your key and save. `.env` is listed in `.gitignore`; logs redact the key automatically.

### Step 6 — Verify UMLS access
```powershell
python scripts\verify_umls_access.py
```
Success: `Authenticated ...` and `CPT ACCESSIBLE`. Exit code 1 means the key works but CPT results are not visible to your account; 2 means the key is missing/invalid or the API is unreachable.

### Step 7 — Initialize DuckDB
```powershell
python scripts\initialize_database.py
```
Creates `database\payer_policy_kb.duckdb` with all tables. Safe to repeat. `--recreate` deletes and recreates the file.

### Step 8 — Obtain UMLS data
```powershell
python scripts\download_umls_data.py
```
Finds the current **Metathesaurus Full Subset** via NLM's Releases API, downloads it with the Download API (resumes if interrupted — just run it again), and extracts MRCONSO, MRSTY, MRREL, MRSAB, MRDEF, MRRANK into `data\raw\META\`. Add `--with-mrsat` for CPT full descriptions. If you downloaded the zip in a browser: `python scripts\download_umls_data.py --zip C:\Downloads\umls-2026AA-metathesaurus-full.zip`.
Do **not** use the "Level 0" subset — it excludes CPT.

### Step 9 — Build the knowledge base
```powershell
python scripts\build_knowledge_base.py
```
Loads the RRF files step by step (each step is one transaction). If it stops, run the same command again: finished steps are skipped. The log ends with table counts, codes per vocabulary and integrity checks (all should say PASS). Options: `--release 2026AA` if auto-detection fails, `--with-attributes` (needs MRSAT), `--replace` when moving to a new UMLS release.

### Step 10 — Map policies to CPT/HCPCS codes
```powershell
python scripts\map_policy_cpt_codes.py --payer Humana
python scripts\map_policy_cpt_codes.py --input data\input\uhc_policies.json --payer UnitedHealthcare
```
Always pass `--payer`: the real file has no payer field (it is otherwise detected from text and recorded as such). Re-running a file replaces that file's results; reviewer decisions are kept. Outputs go to `data\exports\` (see below).

### Step 11 — Validate
```powershell
python -m pytest
python scripts\validate_results.py
python scripts\review_mappings.py export
```
- `pytest` → `44 passed` (uses the synthetic fixture only; no license needed).
- `validate_results.py` → integrity checks + `data\review\gold_annotation_template.csv`. Have a domain expert fill `is_applicable` (yes/no) and **add rows for codes the system missed**, save as `data\review\gold_mappings.csv`, then `python scripts\validate_results.py --gold data\review\gold_mappings.csv` for precision/recall by method and tier.
- `review_mappings.py export` → `data\review\review_queue.csv`. Fill `decision` (approve / reject / add), `reviewer`, `reason`; import with `python scripts\review_mappings.py import data\review\review_queue.csv`.

### Step 12 — Cross-payer comparison
```powershell
python scripts\compare_policies.py
```
Needs policies from at least two payers (Step 10 twice). `--include-same-payer` finds overlapping policies within one payer; `--include-weak` also runs gap analysis on weak pairs.

### Step 13 — Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `UMLS_API_KEY is not set` | Step 5; the file must be named `.env` (not `.env.txt` — enable "File name extensions" in Explorer). |
| `HTTP 401/403`, "rejected the API key" | Key mistyped or expired, or license inactive → UTS *My Profile*; regenerate the key. |
| CPT `NOT VISIBLE`; `missing_code_sabs` warning in the build log | License does not cover CPT, or you used the Level-0 subset. HCPCS still works; CPT inference will be empty. |
| `MRCONSO.RRF not found` | Step 8 not done or files elsewhere → copy RRF files into `data\raw\META` or set `PPI_META_DIR`. |
| HTTP 429 / rate limit | Client retries with back-off and stays under 20 req/s (`UMLS_MAX_RPS` in `.env`). The KB build itself makes no API calls. |
| Network drop during download | Re-run Step 8; it resumes from the `.part` file. |
| Build interrupted / very slow | Re-run Step 9 (resumes). Close other programs; MRREL is the largest step. Don't use `--with-attributes` unless needed. |
| `Schema not initialized` / schema version error | Run Step 7; for a version mismatch back up the `.duckdb` file and use `--recreate`. |
| "database is locked" | Another program (DuckDB CLI, a notebook, VS Code extension) has the file open — close it. |
| `Could not parse ... Unrecognized JSON object` | The file is not a list of policies; see "Input format" in docs/DESIGN.md. `--input` takes a path. |
| Duplicate records | Should not happen (stable keys, transactional rewrites). `validate_results.py` checks; a FAIL row names the table. |
| No inferred codes at all | KB not built (log warns "no CPT/HCPCS codes loaded"), or the policy is administrative (flag `no_procedure_evidence`). See `unmatched_terms.csv`. |
| Many ambiguous candidates | Expected for generic procedure names (e.g. "colonoscopy" → many codes). They are flagged; resolve via review. |
| Gap report says `not_assessable_incomplete_extraction` | The other policy's extraction looks incomplete (truncated paragraphs, no payment-policy section). Fix extraction, then re-run Steps 10 and 12. |

---

## Output files (`data\exports\`)

| File | Content |
|---|---|
| `policy_cpt_mappings.csv` | one row per policy × code (consolidated) |
| `mapping_evidence.json` | every piece of evidence for every mapping |
| `ambiguous_mappings.csv` | ambiguous, conflicting, or inferred-needing-review mappings |
| `code_mentions.csv` | every code-like token found (CPT, HCPCS, modifiers, revenue codes, ICD-10, DRG, unrecognized) with validation status |
| `unmatched_terms.csv` | procedure-like phrases with no KB match (vocabulary gaps) |
| `cpt_reference.csv`, `terminology_terms.csv`, `concept_relationships.csv` | KB extracts limited to codes/concepts used by mappings |
| `matched_policy_pairs.csv` | candidate related policies with score breakdown and scope differences |
| `policy_comparison_report.csv` | per pair × rule type: counts of equivalent / differing / conflicting / one-sided / not-assessable rules |
| `rule_gap_report.csv` | individual findings with both rule texts |
| `unmatched_policies.csv`, `policy_change_impact.csv` | policies with no candidate pair; pairs involving policies whose content changed |
| `mapping_quality_report.md`, `validation_summary.json` | dashboard from actual DB counts; integrity and (with gold data) accuracy |

**Key fields of `policy_cpt_mappings.csv`**
- `code_status`: `explicitly_stated` (code is in the policy text), `inferred` (terminology match), `reviewer_approved`, `reviewer_added` — the three-way distinction required for audit.
- `mapping_origin`: explicit / inferred / explicit+inferred / reviewer_added.
- `evidence_sources`: policy_title, policy_content, policy_table, reviewer.
- `best_method`: explicit_code, exact_preferred_term, exact_synonym, cross_vocab_synonym, normalized_phrase, descriptor_head, hierarchy_group, fuzzy, related_concept.
- `confidence_tier` A–E and `score`: rule-based ranking (definitions in docs/DESIGN.md). **Not probabilities.**
- `scope`: `policy_level` (title/overview evidence) or `section_level` (+ `section_seqs`): an inferred code is *not* assumed to apply to every rule.
- `flags`: e.g. ambiguous_candidate_set, title_content_disagreement, explicit_code_not_in_kb_release, abbreviation_expansion_used, fuzzy_only, related_concept_only, evidence_changed_since_review.
- `code_description`: redacted as `[restricted:CPT srl=3]` unless `PPI_EXPORT_RESTRICTED_STRINGS=true`.

**Sample records** (from the run of your real file against the *synthetic* KB — codes A0001/00021 are fixture codes, shown only to illustrate the format):

```text
policy_title=Screening Colonoscopy  code=A0001 code_status=inferred tier=B score=77.0
  evidence_sources=policy_content;policy_title  best_method=cross_vocab_synonym  cuis=C9900011
  -> title "Screening Colonoscopy" equals a SNOMED CT term on the same UMLS concept as the code
policy_title=Chronic Care Management and Principal Care Management  code=00021 tier=B score=85.0
  best_method=exact_synonym  top_matched_phrase=CCM  flags=abbreviation_expansion_used
  -> "CCM" expanded using the policy's own definition "Chronic Care Management (CCM)"
policy_title=Ambulance Services  code=A0425 code_status=explicitly_stated tier=C
  flags=explicit_code_not_in_kb_release   (the synthetic KB does not contain real codes)
```
A real gap-analysis finding from your file (Humana vs Humana, `--include-same-payer`):
`Laboratory – Hemoglobin A1c` vs `Laboratory – Diabetes Mellitus Testing` → `differs_in_indication_scope`: one limits A1c to once every 365 days for *diagnosis*, the other to once every 3 years for *screening* — reported as a scope difference, not a contradiction.

## Tests (scenarios A–L)

| Scenario | Test |
|---|---|
| A explicit code | `test_code_extraction.py::test_scenario_a_*`, `test_consolidation.py::test_explicit_validated_*` |
| B procedure name, no code | `test_content_mapping.py::test_scenario_b_*` |
| C title only | `test_title_mapping.py::test_scenario_c_title_only` |
| D same code from title + content | `test_consolidation.py::test_scenario_d_*` |
| E title/content disagree | `test_consolidation.py::test_scenario_e_*` |
| F multiple procedures | `test_content_mapping.py::test_scenario_f_*` |
| G ambiguous name | `test_content_mapping.py::test_scenario_g_*` |
| H different payer terminology | `test_policy_comparison.py::test_scenario_h_*` |
| I shared code, different scope | `test_policy_comparison.py::test_scenario_i_*` |
| J incomplete extraction | `test_policy_comparison.py::test_scenario_j_*` |
| K interrupted extraction | `test_umls_extraction.py::test_interrupted_build_*`, `test_download_resumes_with_range` |
| L repeated runs | `test_consolidation.py::test_scenario_l_*` |

Run one file: `python -m pytest tests\test_title_mapping.py -v`. A failing test prints the assertion that broke; nothing in the tests uses licensed data.

## Project layout
```
config/settings.py              all tunables (no secrets)
database/schema.sql             DuckDB schema;  example_queries.sql
src/umls/                       api_client, downloader, rrf_loader (KB build)
src/knowledge_base/             db_manager (connections, transactions, runs), validation
src/policy/                     json_parser, code_extractor, rule_extractor, policy_metadata (quality flags)
src/mapping/                    term_matcher (engine), content_mapper, title_mapper, confidence,
                                mapping_consolidator, pipeline
src/comparison/                 policy_matcher, rule_comparator (gap analysis)
src/reporting/exports.py        CSV/JSON/markdown outputs with license redaction
scripts/                        command-line entry points (Steps 6-12)
tests/                          pytest suite + synthetic UMLS fixture (tests/fixtures/make_fixture.py)
```
