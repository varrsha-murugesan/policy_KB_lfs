# Payer Policy Intelligence — UMLS-Powered CPT Knowledge Base (Python + DuckDB)

Deterministic pipeline (no LLMs, no generative AI) that:

1. Builds a local, release-pinned **terminology knowledge base** in DuckDB from the UMLS Metathesaurus (CPT, HCPCS, and synonyms from other vocabularies on the same concepts);
2. Finds **explicit** CPT/HCPCS codes in payer policies and **infers candidate codes** from policy **content** and, independently, from policy **titles**, with full evidence for every candidate;
3. Identifies **related policies across payers** with an explainable score and scope check;
4. Compares their **rules** and reports differences and possibly missing rules — flagging "not assessable" when an extraction looks incomplete.

Design rationale, repository evaluation, licensing analysis, and schema are in **[docs/DESIGN.md](docs/DESIGN.md)**.
Operational walkthrough and execution runbook: **[docs/END_TO_END_GUIDE.md](docs/END_TO_END_GUIDE.md)** and **[PIPELINE_RUNBOOK.md](PIPELINE_RUNBOOK.md)**.

> **Operational Status:** All code runs deterministically and 60 automated tests pass against a UMLS fixture and synthetic policies. The full pipeline has executed across real policies for UnitedHealthcare (147 policies), Humana (202 policies), and Aetna (35 policies).

---

## Multi-Payer Architecture (Version 2)

Each payer's extraction JSON is ingested through a **payer profile** (`config/payers/*.json`) and an adapter (`guidelines_document`, `sectioned_list`, `declarative`) into a unified canonical model; the mapping, UMLS enrichment, and cross-payer matching engine are fully shared. Profiles exist for **UnitedHealthcare**, **Humana**, and **Aetna**. Adding a new payer is a zero-code configuration task: see **[docs/ADDING_A_PAYER.md](docs/ADDING_A_PAYER.md)**.

```powershell
# 1. Initialize Database
python scripts\initialize_database.py

# 2. Ingest and Map Policies (Run per payer)
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_uhc.json --payer-profile unitedhealthcare
python scripts\map_policy_cpt_codes.py --input data\input\extracted_rules_humana.json --payer-profile humana
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_aetna.json --payer-profile aetna

# 3. Cross-Payer Policy & Rule Gap Analysis
python scripts\compare_policies.py

# 4. Integrity Validation & Regression Tests
python scripts\validate_results.py
python -m pytest tests
```

Audit reports are generated in `data\exports\audit\` (extraction health, unresolved mappings, provenance, code-list policies, quality signals, and section statuses).

---

## Step-by-Step Setup & Execution on Windows

All commands are for **PowerShell**, executed from the `KB creation v1` folder.

### Step 1 — Prerequisites
- **Python 3.10, 3.11, or 3.12 (64-bit)** with `python.exe` on your system PATH.
- **DuckDB**: embedded, handled automatically via `duckdb` Python library.
- **NLM UMLS license & API key** (only needed if downloading raw Metathesaurus releases from NLM).

### Step 2 — Directory Structure & Virtual Environment
```powershell
cd "c:\Users\dell\Documents\Payer Policy\KB creation v1"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Step 3 — Install Dependencies
```powershell
pip install --upgrade pip
pip install -r requirements.txt
```
Dependencies: `duckdb`, `pandas`, `requests`, `python-dotenv`, `rapidfuzz`, `pytest`.

### Step 4 — Initialize Embedded DuckDB
```powershell
python scripts\initialize_database.py
```
Creates `database\payer_policy_kb.duckdb` with all tables, views, and indexes across 4 architectural layers (metadata, terminology, policy corpus, and results). Safe to rerun. Use `--recreate` to wipe and recreate from scratch.

> [!NOTE]
> If `database/payer_policy_kb.duckdb` is already present, all UMLS terminology tables are already loaded. You can proceed directly to mapping policies without needing an active UMLS API key.

### Step 5 — Map Policies to CPT/HCPCS Codes
Ingest source extraction JSON files and map explicit/inferred codes:

```powershell
# UnitedHealthcare
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_uhc.json --payer-profile unitedhealthcare

# Humana
python scripts\map_policy_cpt_codes.py --input data\input\extracted_rules_humana.json --payer-profile humana

# Aetna
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_aetna.json --payer-profile aetna
```

*Optional flags:*
- `--fuzzy-content`: Enables bounded token-sort fuzzy matching on paragraph text.
- `--infer-attachments`: Runs terminology inference on attachment pages.

### Step 6 — Cross-Payer Policy Comparison
Find candidate equivalent policies across payers and detect rule discrepancies:
```powershell
# Cross-payer comparison
python scripts\compare_policies.py

# Optional: also identify overlapping policies within the same payer
python scripts\compare_policies.py --include-same-payer
```

### Step 7 — Structural Validation & Tests
```powershell
# 18 automated structural and referential integrity checks
python scripts\validate_results.py

# Automated pytest regression suite (60 passed, 3 skipped, 0 failures)
python -m pytest tests
```

---

## Output Files (`data\exports\`)

| File | Content |
|---|---|
| `policy_cpt_mappings.csv` | One row per policy × code with confidence tiers A–E, scores, provenance, and flags |
| `mapping_evidence.json` | Complete forensic evidence trail (text snippets, sentence offsets, CUIs, match methods) |
| `ambiguous_mappings.csv` | Ambiguous, conflicting, or inferred mappings requiring review |
| `code_mentions.csv` | Every regex-detected code token (CPT, HCPCS, modifiers, revenue codes, ICD-10) |
| `unmatched_terms.csv` | Clinical procedure phrases with no knowledge base match |
| `cpt_reference.csv` | Unique codes referenced across all mapped policies |
| `matched_policy_pairs.csv` | Candidate related policies across payers with similarity score breakdowns |
| `policy_comparison_report.csv` | Cross-payer matrix of equivalent, differing, conflicting, and one-sided rules |
| `rule_gap_report.csv` | Granular clause-by-clause discrepancy log detailing specific requirement differences |
| `unmatched_policies.csv` | Policies that have no corresponding counterpart in other payers |
| `mapping_quality_report.md` | Executive health dashboard, confidence distributions, and run summaries |
| `audit/audit_summary.md` | Schema validation, section extraction status, and mapping provenance summary |

### Key Fields of `policy_cpt_mappings.csv`
- `code_status`: `explicitly_stated` (code in policy text), `inferred` (terminology match), `reviewer_approved`, `reviewer_added`.
- `mapping_origin`: `explicit`, `supplementary`, `code_list_only`, `inferred`, `reviewer_added`.
- `evidence_sources`: `policy_title`, `policy_content`, `policy_table`, `reviewer`.
- `best_method`: `explicit_code`, `descriptor_head`, `exact_preferred_term`, `exact_synonym`, `cross_vocab_synonym`, `normalized_phrase`, `fuzzy`, `related_concept`.
- `confidence_tier`: **A** (Explicit validated), **B** (Explicit unreferenced or high inference $\ge 75$), **C** (Supplementary / code list $\ge 55$), **D** ($\ge 35$), **E** (< 35).
- `scope`: `policy_level` (title/overview evidence) or `section_level` (+ `section_seqs`).
- `flags`: e.g., `ambiguous_candidate_set`, `title_content_disagreement`, `explicit_code_not_in_kb_release`.

---

## Automated Test Suite

| Test Module | Coverage |
|---|---|
| `test_code_extraction.py` | Explicit code extraction, regex patterns, code table parsing |
| `test_consolidation.py` | Provenance deduplication, multi-source evidence combination |
| `test_content_mapping.py` | Paragraph terminology inference, token-window generation |
| `test_corrected_mapping_rules.py` | Specific regression cases for clinical rules and modifier overrides |
| `test_payer_agnostic.py` | Payer profile registry, adapter polymorphism (UHC, Humana, Aetna) |
| `test_policy_comparison.py` | Policy pair scoring, rule equivalence/differing/conflict classification |
| `test_title_mapping.py` | Title-based concept inference and stopword suppression |
| `test_umls_extraction.py` | RRF batch ingestion, MRCONSO/MRSTY/MRREL parsing, resumable transactions |

Run all tests:
```powershell
python -m pytest tests
```
Output: **60 passed, 3 skipped, 0 failures**.

---

## Directory Organization

```
KB creation v1/
├── config/
│   ├── settings.py              # Central system configuration & paths
│   ├── generic_phrases.json     # Static suppression list to prevent false-positive UMLS matches
│   └── payers/                  # Payer configuration profiles (aetna, humana, unitedhealthcare)
├── database/
│   ├── payer_policy_kb.duckdb   # 196 MB embedded DuckDB analytical database
│   ├── schema.sql               # Database DDL definitions & index scripts
│   └── example_queries.sql      # Diagnostic and reporting SQL queries
├── src/                         # Modular application engine
│   ├── comparison/              # Policy matching (policy_matcher) & rule gap analysis (rule_comparator)
│   ├── enrichment/              # Concept hierarchy and cross-vocabulary enrichment
│   ├── ingest/                  # Adapters (sectioned_list, guidelines_document, declarative) & profile registry
│   ├── knowledge_base/          # Database manager, atomic transactions, 18 integrity validators
│   ├── mapping/                 # Pipeline coordinator, term matching, title/content inference, confidence tiers
│   ├── policy/                  # Raw JSON parsing, regex code extraction, rule pattern recognition
│   ├── reporting/               # CSV/JSON exporters and markdown audit dashboard generators
│   ├── umls/                    # NLM REST/Download client, RRF batch loader
│   └── utils/                   # Logging configuration and SHA-256 stable identifier hashing
├── scripts/                     # Operational CLI commands (Steps 1–7)
├── data/
│   ├── input/                   # Source extraction JSON files (extraction_rules_uhc.json, etc.)
│   ├── raw/                     # UMLS Metathesaurus archive and extracted RRF directory
│   ├── exports/                 # Generated primary CSV, JSON, and Markdown reports
│   │   └── audit/               # Detailed audit logs, unresolved mentions, section coverage
│   └── review/                  # Human-in-the-loop review queues & gold-standard benchmarks
├── docs/                        # Architectural documentation (DESIGN.md, ADDING_A_PAYER.md, END_TO_END_GUIDE.md)
├── tests/                       # 63 automated pytest integration and unit tests
├── requirements.txt             # Python dependencies
└── PIPELINE_RUNBOOK.md          # Quick-start execution guide
```
