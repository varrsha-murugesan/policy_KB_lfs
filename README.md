# Payer Policy Rule Intelligence Pipeline

A deterministic clinical and billing-rule intelligence engine that scrapes, extracts, standardizes, and cross-compares healthcare payer payment policies (UnitedHealthcare, Humana, and Devoted Health). 

The pipeline converts complex, unstructured clinical reimbursement policy PDFs into structured, machine-executable billing rules with **CPT/HCPCS procedure code scoping**, **UMLS-backed ICD-10 diagnosis mappings**, **single-table SQL claim validation queries**, and **cross-payer discrepancy detection**.

---

## 1. High-Level Architecture & Dataflow

```
                    ┌────────────────────────────────────────────────────────┐
                    │            Official Payer Provider Portals             │
                    │      (UnitedHealthcare, Humana, Devoted Health)        │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                                    [Step 1: Scraper]
                                                ▼
                    ┌────────────────────────────────────────────────────────┐
                    │               Raw Policy Document PDFs                 │
                    │               (*.pdf in payer folders)                 │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                                  [Step 2: PDF Extraction]
                                                ▼
                    ┌────────────────────────────────────────────────────────┐
                    │             Structured Extraction JSON                 │
                    │      (extraction_rules_*.json / extracted_rules.json)  │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                          ┌─────────────────────┴─────────────────────┐
                          ▼                                           ▼
            [Step 3: Rule Core & Lexicon]               [Step 4: Clinical Ontology]
            Directive keyword classification            Exact UMLS ICD-10 mapping &
            Statement chunking & clubbing               clinical overrides lookup
                          │                                           │
                          └─────────────────────┬─────────────────────┘
                                                ▼
                                    [Step 5: SQL Skeleton]
                                    Single-table executable SQL
                                    query generation (claim_lines)
                                                ▼
                    ┌────────────────────────────────────────────────────────┐
                    │       Standardized Billing Rules Workbooks (.xlsx)     │
                    │          billing_rules_uhc.xlsx / humana.xlsx          │
                    │  (Rules | Rule_Detail | Code_Map | Query_Logic | Summ) │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                                [Step 6: Cross-Payer Compare]
                                                ▼
                    ┌────────────────────────────────────────────────────────┐
                    │             Cross-Payer Comparison Report              │
                    │               comparison_report.xlsx                   │
                    │   (Rule_Comparison [32 cols] | Unmatched_Rules [19])   │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                                [Step 7: Interactive Demo App]
                                                ▼
                    ┌────────────────────────────────────────────────────────┐
                    │        FastAPI + Tailwind SPA Web Application          │
                    │   (Live Pods | 8-Stage Stepper | DuckDB Claim Testing) │
                    └────────────────────────────────────────────────────────┘
```

---

## 2. Repository Layout

```text
.
├── Demo App/
│   ├── backend/
│   │   ├── app.py                   # FastAPI REST API, SSE streaming, SQLite persistence
│   │   └── jobs.db                  # Local SQLite job state database
│   ├── frontend/
│   │   └── index.html               # SPA frontend (Tailwind CSS, FontAwesome, Chart.js)
│   ├── adapters/
│   │   ├── single_pdf_pipeline.py   # PDF upload adapter, duplicate checking, 8-stage stepper
│   │   └── claim_tester.py          # In-memory DuckDB harness seeded with 50 synthetic claims
│   ├── data/
│   │   ├── demo_samples.json        # Pre-computed sample policies for instant recorded timing replay
│   │   ├── uploads_index.json       # SHA-256 and policy metadata index preventing duplicates
│   │   └── uploads/                 # Temporary storage for uploaded PDFs
│   ├── payers.json                  # Payer configuration registry
│   ├── requirements.txt             # Web app dependencies
│   ├── README.md                    # Demo app setup and feature guide
│   └── test_e2e.py                  # End-to-end verification test suite
│
├── Uhc/
│   ├── scraper.py                   # Scrapes UHC Commercial & MA payment policy PDFs
│   ├── extraction_script.py         # Parses PDF sections, tables, CPT codes, and dates
│   ├── extraction_rules_uhc.json    # 147 extracted UHC policy objects
│   ├── generate_billing_rules.py    # Deterministic rule generator for UnitedHealthcare
│   ├── rule_core.py                 # Lexicon classifier, chunking, and workbook exporter
│   ├── sql_skeleton.py              # SQL dialect generator for claim_lines table
│   ├── icd_lookup.py                # UMLS exact matching & 3-character category expansion
│   ├── icd_overrides.csv            # Override lookup list checked before UMLS
│   ├── claim_table_skeleton.sql     # Target SQL schema for single-table claim queries
│   └── billing_rules_uhc.xlsx       # Output workbook (620 deterministic rules)
│
├── Humana/
│   ├── scraper.py                   # Scrapes Humana payment policy documents
│   ├── extracted_rules.json         # 120 extracted Humana policy objects
│   ├── generate_billing_rules.py    # Deterministic rule generator for Humana
│   ├── rule_core.py                 # Shared lexicon classifier & export routines
│   ├── sql_skeleton.py              # Shared SQL skeleton query generator
│   ├── icd_lookup.py                # Shared UMLS diagnosis mapper
│   ├── icd_overrides.csv            # Shared overrides dictionary
│   ├── claim_table_skeleton.sql     # Target SQL schema for claim queries
│   └── billing_rules_humana.xlsx    # Output workbook (560 deterministic rules)
│
├── devoted_health/
│   ├── scraper.py                   # Web scraper for Devoted Health provider policies
│   ├── extraction_script.py         # Extracts policy text, tables, and frontmatter
│   ├── extraction_rules.json        # 39 extracted Devoted Health policy objects
│   ├── extracted_contents_jsons/    # Per-policy structured extraction files
│   └── policies/                    # Downloaded Devoted Health policy PDFs
│
├── Rule Comparison/
│   ├── compare_rules.py             # Deterministic cross-payer CPT/HCPCS code matcher
│   └── comparison_report.xlsx       # Comprehensive cross-payer alignment & gap report
│
├── KB creation v1/
│   ├── scripts/
│   │   └── build_icd10cm_index.py   # Compiles UMLS RRF clinical index files
│   └── icd10cm_index.sqlite         # SQLite indexed clinical diagnosis ontology
│
├── .gitattributes                   # Git LFS tracking configuration (*.xlsx, *.pdf, *.db, etc.)
└── README.md                        # Master pipeline documentation
```

---

## 3. End-to-End Execution Order & Commands

Follow these steps in numerical order to execute the pipeline from scratch.

### Step 0: Environment Setup

Ensure Python 3.10+ and Git LFS are installed on your machine:

```powershell
# 1. Initialize Git LFS in the repository
git lfs install

# 2. Install required Python libraries
pip install requests beautifulsoup4 pymupdf openpyxl duckdb fastapi uvicorn python-multipart tqdm
```

---

### Step 1: Clinical Knowledge Base Setup (`KB creation v1`)

Build or verify the local ICD-10 clinical ontology index used by `icd_lookup.py` for deterministic diagnosis mapping:

```powershell
cd "c:\Users\dell\Documents\Payer Policy\KB creation v1\scripts"
python build_icd10cm_index.py
cd ..\..
```

---

### Step 2: Policy Scraping

Download the latest publicly accessible policy PDFs from the respective payer provider websites:

```powershell
# UnitedHealthcare
python "Uhc\scraper.py"

# Humana
python "Humana\scraper.py"

# Devoted Health
python "devoted_health\scraper.py"
```

---

### Step 3: PDF Document Parsing & Extraction

Extract text, tables, frontmatter, dates, and procedure code lists from downloaded policy PDFs into structured JSON objects:

```powershell
# UnitedHealthcare (writes extraction_rules_uhc.json)
python "Uhc\extraction_script.py"

# Devoted Health (writes devoted_health\extraction_rules.json)
python "devoted_health\extraction_script.py"
```

---

### Step 4: Deterministic Billing Rule & SQL Query Generation

Run deterministic rule generation for each payer. This step performs:
1. Directive keyword scanning & statement chunking.
2. Attachment code-list mapping (without exploding attachments into thousands of duplicate rules).
3. Exact clinical diagnosis mapping via UMLS and `icd_overrides.csv`.
4. Single-table SQL validation query construction targeting `claim_lines`.
5. Atomic export to multi-sheet Excel workbooks (`.xlsx`) and SQL dumps (`.sql`).

```powershell
# Generate UnitedHealthcare billing rules
python "Uhc\generate_billing_rules.py" --input "Uhc\extraction_rules_uhc.json" --out-dir "Uhc"

# Generate Humana billing rules
python "Humana\generate_billing_rules.py" --input "Humana\extracted_rules.json" --out-dir "Humana"
```

---

### Step 5: Cross-Payer Policy Comparison & Gap Analysis

Execute the cross-payer comparison engine across all generated billing workbooks. This step joins rules by CPT/HCPCS codes, detects outcome discrepancies, and identifies coverage gaps:

```powershell
python "Rule Comparison\compare_rules.py" --inputs "Uhc\billing_rules_uhc.xlsx" "Humana\billing_rules_humana.xlsx" --out "Rule Comparison\comparison_report.xlsx"
```

---

### Step 6: Launch the Interactive Demo Web Application

Start the FastAPI application serving the interactive dark-mode user interface and DuckDB synthetic claim validation engine:

```powershell
cd "Demo App\backend"
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Open your browser at **[http://127.0.0.1:8000](http://127.0.0.1:8000)**.

---

### Step 7: Run End-to-End Automated Verification Test Suite

Run the automated test suite to verify all endpoints, stepper stages, duplicate detection, SQL claim testing, and comparison logic:

```powershell
cd "c:\Users\dell\Documents\Payer Policy"
python "Demo App\test_e2e.py"
```

---

## 4. Standardized Output Formats & Schemas

### 1. Payer Billing Rule Workbook (`billing_rules_<payer>.xlsx`)
Each payer output workbook contains the following structured sheets:
* **`Rules`**: High-level rule catalog with 32 columns including `Rule_ID`, `Policy_Title`, `Policy_Number`, `Line_of_Business`, `Rule_Category`, `Audit_Action`, `Target_Codes`, `Possible_icd_codes`, `Policy_Rule_Text`, `SQL_Query`, and `Review_Flag`.
* **`Rule_Detail`**: Verbatim statement text, source section types, modifiers required/prohibited, bill types, and source PDF reference.
* **`Rule_Code_Map`**: Normalized one-row-per-code mapping resolving parent-child relationships, parameters, and time span thresholds.
* **`Query_Logic`**: Condensed query specifications linking audit actions, target procedure codes, ICD-10 codes, and SQL queries.
* **`Summary`**: Execution metadata, corpus totals, policy counts, and category distribution.

### 2. Single-Table Claim Model (`claim_table_skeleton.sql`)
Every generated SQL query reads exclusively from the unified `claim_lines` table:
```sql
CREATE TABLE claim_lines (
    claim_id                   VARCHAR(50),
    line_num                   INTEGER,
    member_id                  VARCHAR(50),
    member_dob                 DATE,
    member_gender              VARCHAR(50),
    line_of_business           VARCHAR(50),
    claim_type                 VARCHAR(50),
    billing_provider_tin       VARCHAR(50),
    rendering_provider_npi     VARCHAR(50),
    bill_type                  VARCHAR(50),
    place_of_service           VARCHAR(50),
    proc_code                  VARCHAR(50),
    modifier_1                 VARCHAR(50),
    modifier_2                 VARCHAR(50),
    revenue_code               VARCHAR(50),
    diag_code_1                VARCHAR(50),
    diag_code_2                VARCHAR(50),
    service_from_date          DATE,
    service_to_date            DATE,
    units_billed               NUMERIC(10,2),
    allowed_amount             NUMERIC(12,2),
    paid_amount                NUMERIC(12,2),
    line_status                VARCHAR(50),
    PRIMARY KEY (claim_id, line_num)
);
```

### 3. Cross-Payer Comparison Report (`comparison_report.xlsx`)
* **`Rule_Comparison`** (32 columns): Code-overlapping rule pairs classified into:
  - `SAME_RULE`: Identical clinical criteria and audit action.
  - `SAME_CODES_DIFFERENT_RULE_DETAILS`: Overlapping codes with minor parameter differences.
  - `DIFFERENT_DETAILS`: Same code class but divergent clinical indications or modifiers.
  - `POSSIBLE_CONFLICT`: Direct contradiction (e.g. one payer pays while another denies).
* **`Unmatched_Rules`** (19 columns): Rules present in one payer with no counterpart in another, tagged with `Gap_Severity` (`HIGH`, `MEDIUM`, `LOW`).

---

## 5. Git LFS Tracking & Configuration

Large assets (Excel workbooks, PDF policy documents, SQLite databases, DuckDB files) are tracked via Git LFS to keep repository history compact.

### Configured Git LFS Patterns (`.gitattributes`):
```text
*.zip filter=lfs diff=lfs merge=lfs -text
*.pdf filter=lfs diff=lfs merge=lfs -text
*.h5 filter=lfs diff=lfs merge=lfs -text
*.pkl filter=lfs diff=lfs merge=lfs -text
*.parquet filter=lfs diff=lfs merge=lfs -text
*.duckdb filter=lfs diff=lfs merge=lfs -text
*.duckdb.wal filter=lfs diff=lfs merge=lfs -text
*.xlsx filter=lfs diff=lfs merge=lfs -text
*.db filter=lfs diff=lfs merge=lfs -text
*.sqlite filter=lfs diff=lfs merge=lfs -text
```

### Pulling Large Files:
When cloning or pulling the repository, retrieve binary assets with:
```bash
git lfs pull
```
