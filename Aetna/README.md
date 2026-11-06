# Aetna Payment Policy Intelligence Pipeline

A deterministic, verbatim payment policy intelligence and billing rule generation pipeline for **Aetna Better Health / Payment Policy** documents.

---

## 1. Pipeline Overview & Execution Workflow

The Aetna pipeline processes payment policies across three sequential stages:

```mermaid
flowchart LR
    S1["1. scraper.py"] -->|"PDFs"| S2["2. extract_pdf_policies.py"]
    S2 -->|"extraction_rules_aetna.json"| S3["3. generate_billing_rules.py"]
    S3 -->|"Verbatim Rules"| S4["Deliverables (Excel / JSON / CSV)"]
```

### Quick Run Commands (PowerShell)
```powershell
cd "c:\Users\dell\Documents\Payer Policy\Aetna"

# Step 1: Scrape Aetna policy PDFs
python scraper.py

# Step 2: Extract Sections A, B, C, D, and E (guidelines, definitions, tables)
python extract_pdf_policies.py

# Step 3: Generate clean, verbatim billing rules and code linkages
python generate_billing_rules.py
```

---

## 2. Script Inventory & Responsibilities

| Script | Purpose | Key Inputs | Key Outputs |
| :--- | :--- | :--- | :--- |
| **`scraper.py`** | Scrapes policy PDFs from Aetna provider portal | Aetna portal endpoints | `aetna_il_payment_policies/*.pdf` |
| **`extract_pdf_policies.py`** | Multi-section PDF extractor (Sections A, B, C, D, E) | `aetna_il_payment_policies/*.pdf` | `extraction_rules_aetna.json`, `extracted_contents/*.json` |
| **`generate_billing_rules.py`** | Master deterministic rule generator & code mapper | `extraction_rules_aetna.json` | `billing_rules.xlsx`, `billing_rules.json`, `billing_rules.csv` |
| **`policy_lib.py`** | Shared parsing library: deontic keyword extraction, CPT/HCPCS regex, sentence segmentation | — | Core utility library |
| **`claim_requirements.py`** | Maps standard claim fields and review requirements based on rule criteria | — | Core utility library |

---

## 3. Extraction Architecture (Sections A through E)

Aetna payment policies follow a standardized 5-section layout. The extraction engine captures all sections:

- **Section A (Header & Metadata)**: Policy title, policy number (e.g. `ABHIL-RP-0011`), effective dates.
- **Section B (Overview & Clinical Scope)**: Contains clinical screening limitations, unit caps, non-covered services, and fundamental reimbursement principles.
- **Section C (Definitions)**: Authoritative medical and contractual definitions.
- **Section D (Reimbursement Guidelines)**: Detailed clinical billing requirements, modifier usages, unbundling prohibitions, and exceptions.
- **Section E (Codes & Coverage Conditions)**: CPT/HCPCS code tables, modifier lists, and conditions of coverage.

---

## 4. Master Deliverables

| Deliverable | Format | Records | Description |
| :--- | :--- | :--- | :--- |
| **`billing_rules.xlsx`** | Excel Workbook | 212 rules | Master multi-tab workbook with auto-filter and formatting: <br>• `Master Billing Rules`: 212 verbatim rules<br>• `Policy Summary`: Policy-level rule distributions<br>• `Code Index`: 1,423 code linkages across 425 unique codes<br>• `Rule Types Summary`: Breakdown by denial, modifier, limit, etc. |
| **`billing_rules.json`** | JSON Array | 212 rules | Structured machine-readable JSON array |
| **`billing_rules.csv`** | Flat CSV | 212 rules | Tabular export for SQL/data warehouse ingestion |
| **`aetna_pdf_vs_json_audit.xlsx`** | Excel Audit | 35 policies | Complete PDF vs. JSON completeness audit verifying **35 / 35 (100.0%) Complete Extraction** |

---

## 5. Output Schema Reference

The primary billing rules table contains the following columns:

| Column | Type | Example | Description |
| :--- | :--- | :--- | :--- |
| `Rule ID` | String | `AETNA-BR-00001` | Unique sequential identifier |
| `Policy Number` | String | `ABHIL-RP-0011` | Official Aetna policy number |
| `Policy Title` | String | `Allergy Testing and Immunotherapy` | Title of the payment policy |
| `Rule Type` | String | `Denial` | `Denial`, `Reimbursement Criteria`, `Modifier Requirement`, `Bundling/Unbundling`, `Frequency Limit`, etc. |
| `Rule Statement` | String | *(Verbatim text)* | Exact, untouched sentence from the policy document |
| `Incorrect Billing Condition` | String | *(Evaluation condition)* | Specific criterion triggering claim denial or review |
| `Applicable CPT/HCPCS Codes` | String | `95004, 95024` | Comma-delimited list of applicable codes |
| `Applicable Modifiers` | String | `25, 59` | Comma-delimited list of relevant modifiers |
| `Effective Date` | String | `2026-07-01` | Policy effective date |
| `Source Section` | String | `Section D (Reimbursement Guidelines)` | Section origin (`Section B`, `Section C`, `Section D`, `Section E`) |
| `Claim Requirements` | String | `Itemized charge, diagnosis code` | Standard claim fields required for adjudication |

