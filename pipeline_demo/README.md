# Payer Policy Billing Rule Pipeline Demo

A clean, interactive 3-page web platform demonstrating the end-to-end deterministic payer policy intelligence pipeline:
**Policy PDF → Extracted JSON → Billing Rules Workbook (`.xlsx`) → Cross-Payer Rule Comparison.**

---

## 1. Quick Start / How to Run

### Step 1: Environment Setup
Ensure dependencies are installed:
```powershell
pip install -r pipeline_demo/requirements.txt
```

### Step 2: Build Baseline Workbooks (Run Once)
Compile the baseline reference workbooks from existing master policy extractions:
```powershell
python pipeline_demo/scripts/build_baseline.py
```
This generates:
* `pipeline_demo/data/baseline/billing_rules_uhc.xlsx` (539 rules, 2,578 CPT codes)
* `pipeline_demo/data/baseline/billing_rules_humana.xlsx` (587 rules, 482 CPT codes)
* `pipeline_demo/data/baseline/billing_rules_devoted.xlsx` (166 rules, 80 CPT codes)

### Step 3: Launch the Application
Run the FastAPI backend with Uvicorn:
```powershell
uvicorn app:app --app-dir pipeline_demo/backend --host 127.0.0.1 --port 8000
```
Open your browser and navigate to:
```
http://127.0.0.1:8000
```

---

## 2. Directory Layout

```text
pipeline_demo/
├── backend/
│   └── app.py                   # FastAPI REST API, run management, and report parsing
├── frontend/
│   ├── index.html               # Semantic HTML Single Page Application layout
│   ├── styles.css               # Clean light theme (no gradients/glows, 8px radius)
│   └── app.js                   # Vanilla JS router (#/, #/pod/:id, #/pod/:id/compare/:run_id)
├── pods/
│   ├── uhc/                     # United Healthcare ingestion scripts & SQL generator
│   ├── humana/                  # Humana payment policy scripts & SQL generator
│   └── devoted/                 # Devoted Health policy scripts & SQL generator
├── comparison/
│   └── compare_rules.py         # Restored cross-payer code intersection matching engine
├── data/
│   ├── baseline/                # Master pre-built workbooks for the 3 active pods
│   └── runs/                    # Per-upload directories (<run_id>/input, extracted, outputs)
├── scripts/
│   └── build_baseline.py        # Compiles baseline workbooks from canonical extracted JSON
├── requirements.txt             # Python dependencies (FastAPI, PyMuPDF, OpenPyXL, Pandas)
└── README.md                    # Operational runbook and architectural documentation
```

---

## 3. The 3 Pages & Features

### Page 1 — Home (`#/`)
* **KPI Metrics Strip:** Displays Total Rules Extracted (1,292), Total CPT Codes Covered (2,895 distinct codes with ranges expanded), and Total Active Pods (3).
* **Pipeline Step Flow:** Clean horizontal visualization orienting the user across all 6 core transformation phases.
* **Payer Pod Podiums:** Clickable cards for **United Healthcare**, **Humana**, and **Devoted Health** with live rule and code counts.

### Page 2 — PDF Upload & Workspace (`#/pod/<id>`)
* **Drag-and-Drop Zone:** Accepts valid PDF policy documents up to 25 MB with file validation.
* **Vertical Progress Stepper:**
  1. *Upload saved*: Validates file size and `%PDF` magic bytes.
  2. *Extraction (`extraction_script.py`)*: Real subprocess executing PyMuPDF layout parsing. Shows extracted policy/section counts and direct download of `extraction.json`.
  3. *Rule generation (`generate_billing_rules_<client>.py`)*: Real subprocess classifying deontic directives and clubbing rules. Shows rule counts and direct download of `billing_rules_<client>.xlsx`.
  * Every step includes elapsed timing and a collapsible terminal log showing real subprocess stdout/stderr.
* **Interactive Rules Table:**
  * Displays Rule ID, Category, Verbatim Rule Text, Target Codes, Audit Action, and Source Page.
  * Real-time search box and category dropdown filters.
  * Clicking any rule opens a **Side Drawer** showing comprehensive metadata and the dark-themed monospace executable SQL audit query with a one-click copy button.
* **Compare Rules Action Button:** Seamlessly transitions into cross-payer comparison.

### Page 3 — Cross-Payer Compare (`#/pod/<id>/compare/<run_id>`)
* **Execution:** Automatically runs `comparison/compare_rules.py` comparing the new run's workbook against the baseline workbooks of the other 2 pods.
* **Summary Strip:** Summarizes Matched Pairs (`SAME_RULE`), Differing Rules (`DIFFERING_DETAILS`), Conflicts (`POSSIBLE_CONFLICT`), and Unmatched rules, with a direct download button for `comparison_report.xlsx`.
* **Two-Column Side-by-Side View:**
  * **Left Column:** This policy's extracted rules with badges indicating match counts in peer payers.
  * **Right Column:** Matched rules grouped under peer payer headers with color-coded comparison badges (Green for `SAME_RULE`, Amber for `DIFFERING_DETAILS`, Red for `POSSIBLE_CONFLICT`), shared code chips, and difference explanations. If no match exists, displays the exact reason from `Unmatched_Rules`.

---

## 4. Safety & Architecture Principles
* **Isolated Demo Folder:** All scripts and dependencies are completely encapsulated inside `pipeline_demo/`. Original pipeline folders (`Uhc/`, `Humana/`, `devoted_health/`, `KB creation v1/`, `Demo App/`) are 100% untouched.
* **Real Subprocesses:** Zero fake delays or simulated numbers. Every stage reflects real Python process execution.
* **Path Boundary Protection:** All file downloads enforce `is_relative_to(RUNS_DIR)` to strictly prevent directory traversal attacks.
* **Strict CORS & Host Binding:** Bound to `127.0.0.1` with explicit localhost origin allowance.
