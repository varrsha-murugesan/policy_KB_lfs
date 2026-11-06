# UnitedHealthcare (UHC) Payment Policy Intelligence Pipeline

A deterministic, verbatim payment policy intelligence and billing rule generation pipeline for **UnitedHealthcare Commercial and Medicare Advantage** reimbursement policies.

---

## 1. Pipeline Overview & Execution Workflow

The pipeline consists of three sequential, independent stages:

```mermaid
flowchart LR
    S1["1. scraper.py"] -->|"PDFs & Attachments"| S2["2. extraction_script.py"]
    S2 -->|"extraction_rules_uhc.json"| S3["3. generate_billing_rules.py"]
    S3 -->|"Verbatim Rules"| S4["Deliverables (Excel / JSON / CSV)"]
```

### Quick Run Commands (PowerShell)
```powershell
cd "c:\Users\dell\Documents\Payer Policy\Uhc"

# Step 1: Scrape UHC policies and attachment PDFs
python scraper.py

# Step 2: Extract text, tables, and Q&A into structured JSON
python extraction_script.py

# Step 3: Generate clean, verbatim billing rules with attachment parameter in-filling
python generate_billing_rules.py
```

---

## 2. Script Inventory & Responsibilities

| Script | Purpose | Key Inputs | Key Outputs |
| :--- | :--- | :--- | :--- |
| **`scraper.py`** | Scrapes policy PDFs and attachments from UHC provider portal | UHC portal URL endpoints | `commercial_plans/`, `medicare_advantage_plans/`, `policy_attachments/`, `policy_title_uhc.csv` |
| **`extraction_script.py`** | Extracts and parses PDF sections, guideline paragraphs, tables, definitions, and Q&As | Downloaded policy PDFs | `extraction_rules_uhc.json`, `extracted_contents_reimbursement/*.json` |
| **`generate_billing_rules.py`** | Master deterministic rule generator & attachment in-filler | `extraction_rules_uhc.json`, `policy_attachments/` | `billing_rules.xlsx`, `billing_rules.json`, `billing_rules.csv` |
| **`policy_lib.py`** | Shared parsing library: deontic keyword detection, CPT/HCPCS regex, sentence segmentation | — | Core utility library |
| **`claim_requirements.py`** | Maps standard claim fields and review requirements based on rule criteria | — | Core utility library |

---

## 3. Core Operational Principles

1. **Verbatim Policy Text (Zero Hallucinated Words)**:
   Rules extract the untouched sentence text directly from policy guidelines, definitions, tables, and Q&As. No words or interpretations are added.
2. **Deontic Keyword Triggers**:
   Sentences are selected using strict domain triggers:
   - *Denials & Non-Reimbursement*: `not eligible for reimbursement`, `will not be reimbursed`, `not covered`, `denied`, `unbundled`.
   - *Modifiers*: `modifier 25`, `modifier 59`, `modifier 50`, `modifier 78`, `modifier 91`.
   - *Frequency & Limits*: `once per`, `maximum units`, `lifetime limit`, `time span`.
   - *Place of Service & Supervision*: `place of service`, `supervision`, `standby`.
3. **Attachment In-Filling for Parameterized Policies**:
   For policies referencing separate attachment tables, parameter values, time intervals, and code pairings are parsed directly from attachment PDFs and populated into the rule:
   - *Time Span Codes Policy*: In-fills exact lookback days (e.g. 10 days, 90 days, 365 days) and component/comprehensive code pairs from `TimeSpanCodes.pdf`.
   - *Add-on Codes Policy*: In-fills primary and add-on code pairings from `Add-on-to-Primary-Codes.pdf`.
   - *Once in a Lifetime Procedures*: Populates exact lifetime-limited procedure codes from `Once-in-a-Lifetime-Procedures.pdf`.
   - *Age-Based Codes Policy*: Populates eligible pediatric/geriatric age boundaries.
4. **Actionable Incorrect Billing Conditions**:
   Every rule provides a clean, machine-evaluable condition explaining when a submitted claim should be flagged for non-compliance or audit review.

---

## 4. Master Deliverables

| Deliverable | Format | Records | Description |
| :--- | :--- | :--- | :--- |
| **`billing_rules.xlsx`** | Excel Workbook | 1,116 rules | Master multi-tab workbook with styling, auto-filter, and column widths: <br>• `Master Billing Rules`: 1,116 verbatim rules<br>• `Policy Summary`: Policy-level rule distributions<br>• `Code Index`: 438 unique CPT/HCPCS code linkages<br>• `Rule Types Summary`: Breakdown by denial, modifier, limit, etc. |
| **`billing_rules.json`** | JSON Array | 1,116 rules | Structured machine-readable JSON array |
| **`billing_rules.csv`** | Flat CSV | 1,116 rules | Tabular export for SQL/data warehouse ingestion |
| **`billing_rules_verification_audit.xlsx`** | Excel Audit | 1,116 rules | 20-column forensic audit trail: **1,116 / 1,116 (100.0%) Verified Consistent** |
| **`uhc_pdf_vs_json_audit.xlsx`** | Excel Audit | 147 policies | Complete PDF vs. JSON completeness audit verifying 100% substantive rules captured |

---

## 5. Output Schema Reference

The primary billing rules table contains the following columns:

| Column | Type | Example | Description |
| :--- | :--- | :--- | :--- |
| `Rule ID` | String | `UHC-BR-00001` | Unique sequential identifier |
| `Policy Number` | String | `2026R0059C` | Official UHC policy number |
| `Policy Title` | String | `Time Span Codes Policy` | Title of the payment policy |
| `Line of Business` | String | `Commercial` | `Commercial` or `Medicare Advantage` |
| `Rule Type` | String | `Frequency Limit` | `Denial`, `Reimbursement Criteria`, `Modifier Requirement`, `Bundling/Unbundling`, `Frequency Limit`, etc. |
| `Rule Statement` | String | *(Verbatim text)* | Exact, untouched sentence from the policy document |
| `Incorrect Billing Condition` | String | *(Evaluation condition)* | Specific criterion triggering claim denial or review |
| `Applicable CPT/HCPCS Codes` | String | `99213, 99214` | Comma-delimited list of applicable codes |
| `Applicable Modifiers` | String | `25, 59` | Comma-delimited list of relevant modifiers |
| `Effective Date` | String | `2026-01-01` | Policy effective date |
| `Source Section` | String | `Reimbursement Guidelines` | Section origin (`Reimbursement Guidelines`, `Definitions`, `Q&A`) |
| `Attachment Value In-Filled` | String | `Yes / No` | Indicates whether parameter values were extracted from attachment tables |

