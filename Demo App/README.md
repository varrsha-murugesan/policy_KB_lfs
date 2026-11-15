# Payer Policy Rule Intelligence — Demo Web Application

A deterministic payer policy intelligence and cross-payer rule comparison web platform. The platform wraps existing policy pipelines (UnitedHealthcare, Humana, and Devoted Health stub) behind a FastAPI backend and a dark-mode single-page frontend.

---

## 1. Quick Start / How to Run

1. Open a terminal in the project directory:
   ```bash
   cd "c:\Users\dell\Documents\Payer Policy\Demo App\backend"
   ```

2. Run the application using Uvicorn:
   ```bash
   uvicorn app:app --reload --port 8000
   ```

3. Open your browser and navigate to:
   ```
   http://127.0.0.1:8000
   ```

---

## 2. Directory Layout

```text
Demo App/
├── backend/
│   ├── app.py                   # FastAPI REST API, SSE event streams, SQLite job persistence
│   └── jobs.db                  # Local SQLite database persisting background job history
│
├── frontend/
│   └── index.html               # Polished Single Page Application (Tailwind CSS, FontAwesome, Chart.js)
│
├── adapters/
│   ├── single_pdf_pipeline.py   # Adapter for duplicate detection, single-PDF ingestion & atomic runner
│   └── claim_tester.py          # In-memory DuckDB test harness seeded with 50 realistic claim lines
│
├── data/
│   ├── demo_samples.json        # Pre-computed sample policies for instant recorded timing replay
│   ├── uploads_index.json       # SHA-256 and policy metadata index preventing duplicates
│   └── uploads/                 # Storage for uploaded policy PDFs
│
├── payers.json                  # Payer registry configuring pipelines, workbooks, and stubs
├── requirements.txt             # Python package dependencies
└── README.md                    # Setup and architectural documentation
```

---

## 3. Architecture & Core Principles

### Zero Pipeline Rewrites
- **Existing scripts are never modified:** Scripts in `Uhc/`, `Humana/`, and `Rule Comparison/` remain untouched.
- All bridging logic resides cleanly inside `Demo App/adapters/`.

### Atomic State & Concurrency Safety
- **Workbook lock & atomic replace:** During generation, workbooks are written to temporary staging files and replaced atomically via `os.replace`. The Compare page and frontend never observe half-written files.
- **Single-job worker per payer:** Background jobs are guarded by payer-specific locks; queued jobs wait safely.

### Live SSE Progress Streaming
- Background processing steps stream to the browser via Server-Sent Events (`/api/jobs/{job_id}/stream`).
- Running jobs survive browser refreshes via SQLite persistence (`jobs.db`).

---

## 4. Feature Walkthrough

### Page 1 — Home Overview
- **Header & Navigation:** Healthcare tech dark aesthetic, live stats summary strip (Total Payers, Rules, Matched Pairs, Gaps).
- **Payer Pods:** Clickable pods for **UnitedHealthcare**, **Humana**, and **Devoted Health** (clearly badged as `Pipeline not connected - sample data`).
- **Interactive Coverage Heatmap:** Rule density across 12 clinical categories. Clicking any cell opens the payer's workspace pre-filtered to that category.
- **Demo Mode Toggle:** Replays pre-recorded stage timings and sample policies without depending on PDF OCR or extraction speed.

### Page 2 — Payer Workspace
- **Drag & Drop Upload:** Accepts policy PDFs with instant client-side and server-side duplicate check.
- **Duplicate Detection:**
  * Checks SHA-256 file hash and policy numbers against `Rule_Detail` and `uploads_index.json`.
  * Displays warning banner: `This policy already exists (N rules)` with options to **View existing rules** or **Re-process anyway**.
  * Identifies version collisions with a **Replace Version** option.
- **8-Stage Pipeline Stepper:**
  1. Duplicate check
  2. PDF text extraction
  3. Rule identification (keyword classification)
  4. Rule generation (clubbing, codes, attachments)
  5. ICD-10 mapping (UMLS / overrides)
  6. SQL query generation
  7. Cross-payer comparison (background)
  8. Report saved
- **Before / After Counter:** Summarizes net change upon completion (e.g. `+23 rules, +41 codes, +6 new gaps vs Humana`).
- **Output Panel:**
  * KPI cards with rules by category donut chart.
  * Search, category filter, audit action filter, review flag filter, and pagination.
  * Target CPT/HCPCS code chips with `+N more` popover and `Copy all codes` button.
- **Rule Detail Side Drawer:**
  * Verbatim policy rule text.
  * Trigger keywords, target codes, and modifiers.
  * Mapped ICD-10 diagnosis codes.
  * Dark syntax-highlighted runnable SQL query with **Copy SQL** button.
  * **Run on Sample Claims (DuckDB):** Evaluates SQL against 50 synthetic claims in memory and displays flagged claim lines.
- **Artifact Downloads:** Direct download of generated workbooks (`.xlsx`) and queries (`.sql`).

### Page 3 — Compare Rules
- **Instant Load:** Reads directly from `comparison_report.xlsx` with zero recomputation.
- **Scope Selector:** Choose primary payer, comparison payer, and policy scope.
- **Plain-English Headline:** Summary of alignment (e.g. `12 unique rules, 34 similar rules, 9 gaps vs Humana`).
- **Conflict Spotlight:** Card highlighting top 3 `POSSIBLE_CONFLICT` rule pairs with one-sentence risk explanations.
- **Three Ordered Tabs:**
  1. `UNIQUE RULES`: Rules native to primary payer with no match in other payer (grouped by reason & severity).
  2. `SIMILAR RULES`: Code-overlapping rule pairs with color-coded comparison badges (`SAME_RULE`, `SAME_CODES_DIFFERENT_RULE_DETAILS`, `DIFFERENT_DETAILS`, `POSSIBLE_CONFLICT`). Clicking opens a side-by-side discrepancy viewer with highlighted differences.
  3. `RULE GAPS`: Restricted policies or divergent rules from comparison payer sorted by `Gap_Severity` (High -> Low).
- **Export:** Direct export of comparison datasets to Excel.

