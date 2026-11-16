# Payer Policy Intelligence & Billing Rule Pipeline — Comprehensive Handover

**Document Version:** 1.0.0  
**Last Updated:** November 16, 2026  
**Repository Remote:** `https://github.com/varrsha-murugesan/policy_KB_lfs.git` (`main` branch)  
**System Architecture:** Python 3.10+, FastAPI, DuckDB, UMLS Metathesaurus, OpenPyXL, Git LFS  

---

## 1. Executive Summary

The **Payer Policy Intelligence Platform** is an enterprise-grade, deterministic (zero LLM hallucination) data pipeline and interactive analytics web application for ingesting, standardizing, evaluating, and cross-comparing healthcare reimbursement and payment policies.

### Core Capabilities
1. **Multi-Payer Policy Scraping & Extraction:** Scrapes and extracts unstructured PDF policies from major healthcare payers (UnitedHealthcare, Humana, Devoted Health) into structured, section-aware JSON representations.
2. **Deterministic Billing Rule Generation:** Compiles verbatim policy requirements into standardized, tabular billing rules using deontic keyword analysis, code extraction (CPT, HCPCS, REV, ICD-10, Modifiers), and attachment value resolution.
3. **Clinical Ontology & ICD-10 Mapping:** Clinical condition normalization backed by the Unified Medical Language System (UMLS Metathesaurus in DuckDB) and a verified override taxonomy (`icd_overrides.csv`).
4. **Single-Table SQL Dialect Generation:** Automatically generates synthetically testable SQL queries targeting a normalized `claim_lines` schema for instant claim scrubbing.
5. **Cross-Payer Rule Comparison & Gap Analysis:** Deterministically pairs rules across payers via CPT/HCPCS code intersection and ICD-10 clinical similarity, categorizing findings into *Equivalence*, *Differing Criteria*, *Direct Conflicts*, and *Payer Gaps*.
6. **Full-Featured Interactive Web Application:** A unified single-page application (SPA) with a FastAPI backend featuring live payer pods, an 8-stage progress stepper, SHA-256 duplicate guards, DuckDB synthetic claim execution, and interactive comparison dashboards.

---

## 2. Clean Repository Layout

Following the pruning of obsolete legacy folders, directory junction fixes, and the removal of Aetna, the repository is structured as a clean root workspace:

```text
c:\Users\dell\Documents\Payer Policy\
│
├── Demo App/                          # Interactive Web Platform
│   ├── backend/
│   │   ├── app.py                     # FastAPI REST API, SSE streaming, SQLite persistence
│   │   └── jobs.db                    # Local SQLite job state database (Git LFS)
│   ├── frontend/
│   │   └── index.html                 # Responsive SPA (Tailwind CSS, FontAwesome, Chart.js)
│   ├── adapters/
│   │   ├── single_pdf_pipeline.py     # PDF upload adapter, SHA-256 duplicate guard, 8-stage stepper
│   │   └── claim_tester.py            # DuckDB claim evaluation engine (seeded with 50 claims)
│   ├── data/
│   │   ├── demo_samples.json          # Pre-computed sample policies for instant timing replay
│   │   ├── uploads_index.json         # SHA-256 and policy metadata index
│   │   └── uploads/                   # Ephemeral upload storage for policy processing
│   ├── payers.json                    # Dynamic payer configuration registry
│   ├── requirements.txt               # Web application dependencies
│   ├── README.md                      # Dedicated Demo App documentation
│   └── test_e2e.py                    # 5-stage automated end-to-end verification suite
│
├── Uhc/                               # UnitedHealthcare Pipeline
│   ├── scraper.py                     # PDF scraper for UHC Commercial & MA payment policies
│   ├── extraction_script.py           # Multi-section PDF parser (text, tables, codes, dates)
│   ├── extraction_rules_uhc.json      # 147 extracted UHC policy objects
│   ├── generate_billing_rules.py      # Master deterministic rule generator
│   ├── rule_core.py                   # Lexicon classifier, chunking, and Excel exporter
│   ├── sql_skeleton.py                # SQL dialect generator targeting claim_lines table
│   ├── icd_lookup.py                  # UMLS Metathesaurus lookup & 3-character expansion
│   ├── icd_overrides.csv              # High-priority clinical override dictionary
│   ├── claim_table_skeleton.sql       # Target SQL schema definition
│   ├── billing_rules_uhc.xlsx         # Master generated workbook (620 deterministic rules)
│   ├── commercial_plans/              # Raw scraped Commercial policy PDFs
│   ├── medicare_advantage_plans/      # Raw scraped Medicare Advantage policy PDFs
│   └── policy_attachments/            # Raw scraped policy attachment tables & code lists
│
├── Humana/                            # Humana Pipeline
│   ├── scraper.py                     # Scraper for Humana payment integrity policies
│   ├── extracted_rules.json           # 120 extracted Humana policy objects
│   ├── generate_billing_rules.py      # Master deterministic rule generator
│   ├── rule_core.py                   # Shared lexicon classifier & workbook exporter
│   ├── sql_skeleton.py                # Shared SQL query generator
│   ├── icd_lookup.py                  # Shared UMLS diagnosis mapper
│   ├── icd_overrides.csv              # High-priority clinical override dictionary
│   ├── claim_table_skeleton.sql       # Target SQL schema definition
│   ├── billing_rules_humana.xlsx      # Master generated workbook (560 deterministic rules)
│   └── payment policy/                # Policy source documents, archive PDFs & scraper
│
├── devoted_health/                    # Devoted Health Pipeline
│   ├── scraper.py                     # BeautifulSoup policy scraper with retry/resume logic
│   ├── extraction_script.py           # Table-aware PDF extraction engine
│   ├── extraction_rules.json          # 39 extracted Devoted Health policy objects
│   ├── extracted_contents_jsons/      # Per-policy granular extraction files
│   └── policies/                      # 39 downloaded policy PDF documents
│
├── Rule Comparison/                   # Cross-Payer Comparison Engine
│   ├── compare_rules.py               # Deterministic CPT/HCPCS code matcher and reporter
│   ├── comparison_report.xlsx         # Master comparison report (32-col matched, 19-col gaps)
│   ├── ingest.py                      # Multi-payer rule normalizer
│   ├── matcher.py                     # Pairwise rule matching algorithms
│   ├── gap_finder.py                  # Policy & code gap identification
│   ├── reporter.py                    # Multi-tab Excel and markdown report generator
│   └── README.md                      # Comparison methodology documentation
│
├── KB creation v1/                    # UMLS Clinical Knowledge Base
│   ├── database/                      # DuckDB Metathesaurus instance (`umls.duckdb`)
│   ├── scripts/
│   │   ├── initialize_database.py     # Schema creation and table indexing
│   │   ├── build_icd10cm_index.py     # ICD-10-CM concept indexer
│   │   ├── map_policy_cpt_codes.py    # Policy CPT/HCPCS mapper
│   │   ├── compare_policies.py        # Cross-payer semantic policy comparator
│   │   └── validate_results.py        # 18 relational invariant verification script
│   ├── PIPELINE_RUNBOOK.md            # Detailed KB command runbook
│   └── README.md                      # Knowledge base architecture guide
│
├── .gitattributes                     # Git LFS tracking rules (*.xlsx, *.db, *.pdf)
├── .gitignore                         # Python cache, virtualenv, and local temp rules
├── README.md                          # Master pipeline documentation
└── HANDOVER.md                        # This document
```

---

## 3. Payer Pipeline Specifications

### 3.1 UnitedHealthcare (UHC)
- **Status:** Connected & fully operational.
- **Policies Extracted:** 147 policies (Commercial & Medicare Advantage).
- **Billing Rules Generated:** 620 rules (`Uhc/billing_rules_uhc.xlsx`).
- **Distinct Codes Mapped:** 5,936 CPT/HCPCS codes.
- **Generated Workbook Sheets:**
  1. `Rules`: High-level rule catalog (Rule ID, Policy Number, Title, Category, Action).
  2. `Rule_Detail`: Full verbatim clause text, exceptions, lookback periods, frequency caps.
  3. `Rule_Code_Map`: Normalized 1-to-1 mapping of Rule ID to CPT/HCPCS, Modifiers, and Revenue codes.
  4. `Query_Logic`: Executable DuckDB SQL query snippets targeting `claim_lines`.
  5. `Summary`: Macro statistics, category distributions, and code coverage metrics.

### 3.2 Humana
- **Status:** Connected & fully operational.
- **Policies Extracted:** 120 policies.
- **Billing Rules Generated:** 560 rules (`Humana/billing_rules_humana.xlsx`).
- **Distinct Codes Mapped:** 371 CPT/HCPCS codes.
- **Generated Workbook Sheets:** Identical 5-sheet schema matching UHC for seamless cross-payer alignment.

### 3.3 Devoted Health
- **Status:** Scraped & Extracted (Stubbed in UI as `"Pipeline not connected - sample data"`).
- **Policies Discovered:** 39 policy PDFs downloaded to `devoted_health/policies/`.
- **Extraction Rules:** 39 structured policy JSON objects in `devoted_health/extraction_rules.json`.
- **Granular JSONs:** 39 per-policy extractions in `devoted_health/extracted_contents_jsons/`.
- **Next Development Step:** Implement `devoted_health/generate_billing_rules.py` using `rule_core.py` and `sql_skeleton.py` to transition from stub to connected status.

---

## 4. Cross-Payer Comparison Platform (`Rule Comparison`)

The comparison engine ingests generated workbooks across payers and performs pairwise intersection analysis.

```text
┌───────────────────────────┐       ┌───────────────────────────┐
│ Uhc/billing_rules_uhc.xlsx│       │Humana/billing_rules_humana│
└─────────────┬─────────────┘       └─────────────┬─────────────┘
              │                                   │
              └───────────────┬───────────────────┘
                              ▼
               [Rule Comparison/compare_rules.py]
                              │
              ┌───────────────┴───────────────────┐
              ▼                                   ▼
┌───────────────────────────┐       ┌───────────────────────────┐
│ Sheet 1: Rule_Comparison  │       │  Sheet 2: Unmatched_Rules │
│ 32 columns | Matched Pairs│       │  19 columns | Policy Gaps │
│ Tier: STRONG / POSSIBLE   │       │  Severity: HIGH / MEDIUM  │
└───────────────────────────┘       └───────────────────────────┘
```

### Metrics & Findings
- **Total Matched Candidate Pairs:** 1,898 pairs.
- **Total Unmatched Rule Gaps:** 1,135 gaps.
- **Comparison Taxonomy:**
  - `EQUIVALENT`: Both payers require identical actions for the same clinical code/scenario.
  - `DIFFERING`: Payers match on codes but apply different lookback periods, frequency caps, or modifier requirements.
  - `CONFLICTING`: One payer allows/reimburses the service under specific criteria while the other issues an absolute line denial (`DENY_LINE`).
  - `GAP`: A rule exists in one payer's policy manual with no equivalent rule published by the competing payer.

---

## 5. Clinical Knowledge Base & ICD-10 Pipeline (`KB creation v1`)

The Clinical Knowledge Base resolves clinical diagnoses and conditions extracted from policy text against the UMLS Metathesaurus and ICD-10-CM.

### Key Logic & Refinements
1. **Lead-In Condition Extraction:** If a policy bullet or list item specifies a procedure or test name (e.g. CPT code, "measurement", or "test"), the condition is automatically extracted from the sentence lead-in (e.g., following *"diagnosis of"*, *"individuals with"*, *"suspected of having"*).
2. **Strict 3-Character Category Expansion:** Expansion to 3-character root categories occurs **ONLY** when the matched ICD-10 code's descriptor explicitly contains `"unspecified"` (e.g., `J11.1`, `E03.9`, `J06.9`). Specific codes (e.g., `R53.83` fatigue, `R68.83` chills) remain unexpanded.
3. **Priority Override Dictionary (`icd_overrides.csv`):** Evaluated **BEFORE** querying UMLS. Seeded with critical high-frequency clinical conditions (e.g., Hepatitis C: `B17.10`, `B17.11`, `B18.2`, `B19.20`, `B19.21`). Rows populated via override are explicitly tagged as `"ICD-10 from override list - verify"`.

---

## 6. Interactive Demo Web Application (`Demo App`)

### Architecture
- **Backend:** FastAPI running asynchronous endpoints on Python 3.10.
- **Job Persistence:** SQLite database (`Demo App/backend/jobs.db`) preserving job state across page reloads.
- **Streaming:** Server-Sent Events (SSE) via `/api/jobs/{job_id}/events` pushing live stepper updates to the browser.
- **In-Memory Claim Tester:** DuckDB engine loaded with a pre-seeded synthetic claims dataset (50 test claims) executing the generated SQL queries in real time.

### UI Views
1. **Page 1 — Home Dashboard:**
   - Dark healthcare-tech UI theme.
   - Payer Pods: Live policy counts, rule counts, distinct codes, and last-updated timestamps read from workbooks.
   - Summary Strip: Total payers, total rules, matched pairs, total gaps.
   - Cross-Payer Policy Heatmap: 12 clinical categories across payers.
2. **Page 2 — Payer Workspace:**
   - Drag-and-drop PDF upload zone.
   - Step 1: SHA-256 duplicate detection checking against `uploads_index.json` and existing workbook metadata. Prevents redundant re-runs unless explicitly overridden.
   - Step 2: 8-Stage Progress Stepper:
     1. Duplicate check
     2. PDF text extraction
     3. Rule identification
     4. Rule generation
     5. ICD-10 mapping
     6. SQL query generation
     7. Cross-payer comparison
     8. Report saved
   - Output Drawer: KPI cards, category donut chart, rules requiring attachment lookup, interactive rules table, and executable SQL query inspector with DuckDB execution results.
3. **Page 3 — Comparison & Conflict Spotlight:**
   - Filterable side-by-side rule comparison table.
   - Direct conflict spotlight highlighting opposing denial vs. payment criteria.
   - Rule gap explorer with severity classification.

---

## 7. Execution Runbook & Command Sequence

All commands should be executed from the project root: `c:\Users\dell\Documents\Payer Policy\`

```powershell
# ==============================================================================
# 1. ENVIRONMENT SETUP
# ==============================================================================
# Activate Python environment
python -m pip install -r "Demo App/requirements.txt"

# ==============================================================================
# 2. RUN PAYER BILLING RULE GENERATORS
# ==============================================================================
# UnitedHealthcare Rule Generation (Generates Uhc/billing_rules_uhc.xlsx)
python Uhc/generate_billing_rules.py

# Humana Rule Generation (Generates Humana/billing_rules_humana.xlsx)
python Humana/generate_billing_rules.py

# ==============================================================================
# 3. RUN CROSS-PAYER COMPARISON
# ==============================================================================
# Generates Rule Comparison/comparison_report.xlsx
python "Rule Comparison/compare_rules.py"

# ==============================================================================
# 4. RUN CLINICAL KNOWLEDGE BASE PIPELINE (UMLS + DuckDB)
# ==============================================================================
cd "KB creation v1"
python scripts\initialize_database.py
python scripts\build_icd10cm_index.py
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_uhc.json --payer-profile unitedhealthcare
python scripts\map_policy_cpt_codes.py --input data\input\extracted_rules_humana.json --payer-profile humana
python scripts\compare_policies.py
python scripts\validate_results.py
python -m pytest tests
cd ..

# ==============================================================================
# 5. RUN AUTOMATED END-TO-END VERIFICATION SUITE
# ==============================================================================
# Runs 5 stages of API, ingestion, DuckDB claims testing, and duplicate checks
python "Demo App/test_e2e.py"

# ==============================================================================
# 6. LAUNCH INTERACTIVE WEB APPLICATION
# ==============================================================================
# Launches web server on http://127.0.0.1:8000
python -m uvicorn app:app --app-dir "Demo App/backend" --host 127.0.0.1 --port 8000 --reload
```

---

## 8. Git & Git LFS Configuration

All large binary files, Excel workbooks, SQLite databases, and sample PDFs are tracked under **Git Large File Storage (Git LFS)**.

### Tracked LFS Patterns (`.gitattributes`)
```gitattributes
*.xlsx filter=lfs diff=lfs merge=lfs -text
*.db filter=lfs diff=lfs merge=lfs -text
*.sqlite filter=lfs diff=lfs merge=lfs -text
*.pdf filter=lfs diff=lfs merge=lfs -text
```

### Git Verification Commands
```powershell
# Verify Git LFS status
git lfs status

# Verify tracked LFS files
git lfs ls-files

# Standard Git sync
git pull origin main
git push origin main
```

---

## 9. Recent Changes & Fixes Log

| Commit | Description |
| :--- | :--- |
| **`fe22412`** | Documented full KB creation v1 pipeline commands and execution runbook in `PIPELINE_RUNBOOK.md` and `README.md`. |
| **`1d034a8`** | **Folder Structure Consolidation:** Removed accidental `Pods/` filesystem directory and broken Windows NTFS directory junctions (`devoted_health`, `Humana`, `Uhc`). Restored all payer directories cleanly at project root without unwanted directory depth. |
| **`c154d31`** | **Aetna Removal:** Pruned obsolete `Aetna/` directory, removed Aetna from `Rule Comparison/ingest.py` active sources, removed Aetna from `KB creation v1` configs, and updated all documentation. |

---

## 10. Handover Checklist & Next Recommendations

- [x] All payer directories flattened and verified at root (`Uhc`, `Humana`, `devoted_health`).
- [x] Zero broken junctions or duplicate folders present on disk.
- [x] Aetna fully decommissioned from pipelines, comparison scripts, and docs.
- [x] Demo App backend and frontend 100% verified via automated test suite (`test_e2e.py`).
- [x] Master Git repository synchronized and up-to-date with `origin/main`.
- [ ] **Recommended Next Task:** Develop `devoted_health/generate_billing_rules.py` to compile Devoted Health's 39 extracted policies into a `billing_rules_devoted.xlsx` workbook, transitioning Devoted Health from stub badge to fully active status.
