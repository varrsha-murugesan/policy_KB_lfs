"""
app.py
======
FastAPI Backend for Payer Policy Rule Intelligence Demo App.
Serves REST API, SSE streaming, DuckDB synthetic claim execution,
and static frontend. Run with:
  uvicorn app:app --reload
from Demo App/backend or from project root.
"""

import os
import sys
import json
import time
import uuid
import sqlite3
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import Counter

import openpyxl
from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent
DEMO_APP_DIR = BACKEND_DIR.parent
PROJECT_ROOT = DEMO_APP_DIR.parent

# Add adapters to sys.path
sys.path.insert(0, str(DEMO_APP_DIR / "adapters"))
from claim_tester import get_claim_tester
from single_pdf_pipeline import (
    check_duplicate, run_pipeline, get_current_stats,
    compute_sha256, UPLOADS_DIR, DEMO_SAMPLES_PATH,
    PROJECT_ROOT, PAYERS_CONFIG_PATH, load_uploads_index
)

# SQLite setup for persisting jobs
DB_PATH = BACKEND_DIR / "jobs.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            payer_id TEXT,
            filename TEXT,
            file_path TEXT,
            sha256 TEXT,
            status TEXT,
            current_stage INTEGER,
            progress_pct INTEGER,
            is_demo INTEGER,
            stages_json TEXT,
            before_after_json TEXT,
            error_message TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def save_job_db(job: Dict[str, Any]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO jobs (
            id, payer_id, filename, file_path, sha256, status,
            current_stage, progress_pct, is_demo, stages_json,
            before_after_json, error_message, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job["id"],
        job.get("payer_id", ""),
        job.get("filename", ""),
        job.get("file_path", ""),
        job.get("sha256", ""),
        job.get("status", "pending"),
        job.get("current_stage", 1),
        job.get("progress_pct", 0),
        1 if job.get("is_demo") else 0,
        json.dumps(job.get("stages", [])),
        json.dumps(job.get("before_after", {})),
        job.get("error", ""),
        job.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        time.strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()

def get_job_db(job_id: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["stages"] = json.loads(d["stages_json"]) if d["stages_json"] else []
    d["before_after"] = json.loads(d["before_after_json"]) if d["before_after_json"] else {}
    d["is_demo"] = bool(d["is_demo"])
    return d

# In-memory job state cache
MEMORY_JOBS: Dict[str, Dict[str, Any]] = {}

# In-memory caches for instant sub-second API responses
_CACHE_PAYERS: Dict[str, Any] = {"data": None, "ts": 0}
_CACHE_SUMMARY: Dict[str, Any] = {"data": None, "ts": 0}
_CACHE_HEATMAP: Dict[str, Any] = {"data": None, "ts": 0}

def invalidate_caches():
    _CACHE_PAYERS["ts"] = 0
    _CACHE_SUMMARY["ts"] = 0
    _CACHE_HEATMAP["ts"] = 0

app = FastAPI(title="Payer Policy Rule Intelligence API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_payers_registry() -> List[Dict[str, Any]]:
    with open(PAYERS_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/payers")
def get_payers():
    now = time.time()
    if _CACHE_PAYERS["data"] and (now - _CACHE_PAYERS["ts"] < 30):
        return _CACHE_PAYERS["data"]

    payers = load_payers_registry()
    result = []
    for p in payers:
        item = dict(p)
        item["policies_count"] = 0
        item["rules_count"] = 0
        item["distinct_codes"] = 0
        item["last_updated"] = None

        if p["status"] == "connected" and p.get("output_workbook"):
            wb_path = PROJECT_ROOT / p["output_workbook"]
            if wb_path.exists():
                try:
                    mtime = os.path.getmtime(wb_path)
                    item["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    wb = openpyxl.load_workbook(wb_path, read_only=True)
                    if "Rules" in wb.sheetnames:
                        ws = wb["Rules"]
                        rows = list(ws.iter_rows(values_only=True))
                        if len(rows) > 1:
                            headers = rows[0]
                            item["rules_count"] = len(rows) - 1
                            # Distinct policies
                            pol_idx = headers.index("Policy_Number") if "Policy_Number" in headers else -1
                            if pol_idx >= 0:
                                item["policies_count"] = len(set(r[pol_idx] for r in rows[1:] if r[pol_idx]))
                            # Distinct codes
                            code_idx = headers.index("Target_Codes") if "Target_Codes" in headers else -1
                            if code_idx >= 0:
                                codes = set()
                                for r in rows[1:]:
                                    c_str = str(r[code_idx] or "")
                                    for c in c_str.split(","):
                                        c_clean = c.strip()
                                        if c_clean:
                                            codes.add(c_clean)
                                item["distinct_codes"] = len(codes)
                except Exception as e:
                    print(f"Error reading workbook for {p['id']}: {e}")
        elif p["status"] == "stub":
            ext_json = PROJECT_ROOT / p["extraction_json"]
            if ext_json.exists():
                try:
                    mtime = os.path.getmtime(ext_json)
                    item["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    with open(ext_json, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        item["policies_count"] = len(d)
                        item["rules_count"] = 0
                        item["distinct_codes"] = 0
                except Exception as e:
                    print(f"Error reading stub {p['id']}: {e}")
        result.append(item)

    _CACHE_PAYERS["data"] = result
    _CACHE_PAYERS["ts"] = now
    return result


@app.get("/api/summary")
def get_summary():
    """Summary strip: total payers, total rules, total matched pairs, total gaps."""
    now = time.time()
    if _CACHE_SUMMARY["data"] and (now - _CACHE_SUMMARY["ts"] < 30):
        return _CACHE_SUMMARY["data"]

    payers = get_payers()
    total_payers = len(payers)
    total_rules = sum(p.get("rules_count", 0) for p in payers)

    total_matched_pairs = 0
    total_gaps = 0

    comp_report = PROJECT_ROOT / "Rule Comparison" / "comparison_report.xlsx"
    if comp_report.exists():
        try:
            wb = openpyxl.load_workbook(comp_report, read_only=True)
            if "Rule_Comparison" in wb.sheetnames:
                ws_comp = wb["Rule_Comparison"]
                rows = list(ws_comp.iter_rows(values_only=True))
                if len(rows) > 1:
                    total_matched_pairs = (len(rows) - 1) // 2
            if "Unmatched_Rules" in wb.sheetnames:
                ws_unm = wb["Unmatched_Rules"]
                rows = list(ws_unm.iter_rows(values_only=True))
                if len(rows) > 1:
                    total_gaps = len(rows) - 1
        except Exception as e:
            print(f"Error reading summary from comp report: {e}")

    res = {
        "total_payers": total_payers,
        "total_rules": total_rules,
        "total_matched_pairs": total_matched_pairs,
        "total_gaps": total_gaps
    }
    _CACHE_SUMMARY["data"] = res
    _CACHE_SUMMARY["ts"] = now
    return res


@app.get("/api/heatmap")
def get_coverage_heatmap():
    """Coverage heatmap: payers x rule category matrix."""
    now = time.time()
    if _CACHE_HEATMAP["data"] and (now - _CACHE_HEATMAP["ts"] < 30):
        return _CACHE_HEATMAP["data"]

    payers = ["uhc", "humana", "devoted"]
    payer_display = {
        "uhc": "UnitedHealthcare",
        "humana": "Humana",
        "devoted": "Devoted Health"
    }

    standard_categories = [
        "REIMBURSEMENT_CRITERIA",
        "DENIAL_PROHIBITION",
        "MODIFIER_RULE",
        "BUNDLING_INCLUSION",
        "FREQUENCY_LIMIT",
        "PAYMENT_ADJUSTMENT",
        "PLACE_OF_SERVICE",
        "ADD_ON_PRIMARY",
        "AGE_GENDER_RESTRICTION",
        "BILL_TYPE_REVENUE_CODE",
        "ONCE_IN_A_LIFETIME",
        "EXCEPTION_OVERRIDE"
    ]

    matrix: Dict[str, Dict[str, int]] = {
        p: {cat: 0 for cat in standard_categories} for p in payers
    }

    for p in ["uhc", "humana"]:
        wb_rel = "Uhc/billing_rules_uhc.xlsx" if p == "uhc" else "Humana/billing_rules_humana.xlsx"
        wb_path = PROJECT_ROOT / wb_rel
        if wb_path.exists():
            try:
                wb = openpyxl.load_workbook(wb_path, read_only=True)
                ws = wb["Rules"]
                rows = list(ws.iter_rows(values_only=True))
                if len(rows) > 1:
                    h = rows[0]
                    cat_idx = h.index("Rule_Category") if "Rule_Category" in h else -1
                    if cat_idx >= 0:
                        for r in rows[1:]:
                            raw_cat = str(r[cat_idx] or "").strip()
                            for sc in standard_categories:
                                if sc in raw_cat:
                                    matrix[p][sc] += 1
            except Exception as e:
                print(f"Error computing heatmap for {p}: {e}")

    out = {
        "categories": standard_categories,
        "payers": [
            {"id": p, "name": payer_display[p], "counts": matrix[p]}
            for p in payers
        ]
    }
    _CACHE_HEATMAP["data"] = out
    _CACHE_HEATMAP["ts"] = now
    return out


@app.get("/api/rules/{payer_id}")
def get_rules(
    payer_id: str,
    category: Optional[str] = None,
    audit_action: Optional[str] = None,
    lob: Optional[str] = None,
    review_flag: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    """Returns rules for a payer with search, filter, KPIs, and donut breakdown."""
    if payer_id == "devoted":
        # Devoted Health stub - sample extracted policies
        ext_json = PROJECT_ROOT / "devoted_health" / "extraction_rules.json"
        if ext_json.exists():
            with open(ext_json, "r", encoding="utf-8") as f:
                policies = json.load(f)
            sample_rules = []
            for i, pol in enumerate(policies[:20]):
                sample_rules.append({
                    "Rule_ID": f"DEV-{pol.get('policy_number', 'POL')}-R01",
                    "Payer": "Devoted Health",
                    "Policy_Title": pol.get("policy_title", "Sample Policy"),
                    "Policy_Number": pol.get("policy_number", f"DEV-{i+1:03d}"),
                    "Line_of_Business": pol.get("line_of_business", "Medicare Advantage"),
                    "Billing_Class": pol.get("billing_class", "Professional"),
                    "Effective_Date": pol.get("effective_date", "01/01/2026"),
                    "Rule_Category": "REIMBURSEMENT_CRITERIA",
                    "Audit_Action": "PAY_IF_CRITERIA_MET",
                    "Target_Codes": "Sample CPTs",
                    "Possible_icd_codes": "N/A",
                    "Code_Count": 0,
                    "Policy_Rule_Text": pol.get("source_url") or "Devoted Health policy criteria extracted from official provider portal.",
                    "SQL_Query": "-- Devoted Health: Pipeline not connected - sample data\nSELECT * FROM claim_lines WHERE line_of_business = 'MEDICARE_ADVANTAGE';",
                    "Review_Flag": "STUB_SAMPLE"
                })
            return {
                "total": len(sample_rules),
                "page": 1,
                "limit": limit,
                "rules": sample_rules,
                "kpis": {
                    "total_rules": len(sample_rules),
                    "distinct_codes": 0,
                    "review_needed_count": 0,
                    "categories_breakdown": {"REIMBURSEMENT_CRITERIA": len(sample_rules)}
                },
                "is_stub": True
            }

    wb_rel = "Uhc/billing_rules_uhc.xlsx" if payer_id == "uhc" else "Humana/billing_rules_humana.xlsx"
    wb_path = PROJECT_ROOT / wb_rel

    if not wb_path.exists():
        raise HTTPException(status_code=404, detail=f"Workbook not found for payer {payer_id}")

    try:
        wb = openpyxl.load_workbook(wb_path, read_only=True)
        ws = wb["Rules"]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return {"total": 0, "page": page, "limit": limit, "rules": [], "kpis": {}}

        headers = [str(h).strip() if h else "" for h in rows[0]]
        all_rules = []
        for r in rows[1:]:
            rule_dict = dict(zip(headers, r))
            all_rules.append(rule_dict)

        # Compute KPIs
        distinct_codes = set()
        review_count = 0
        cat_counts = Counter()

        for r in all_rules:
            c_str = str(r.get("Target_Codes") or "")
            for c in c_str.split(","):
                c_clean = c.strip()
                if c_clean:
                    distinct_codes.add(c_clean)
            rf = str(r.get("Review_Flag") or "").strip()
            if rf and rf.lower() not in ("none", ""):
                review_count += 1
            cat = str(r.get("Rule_Category") or "OTHER").strip()
            cat_counts[cat] += 1

        kpis = {
            "total_rules": len(all_rules),
            "distinct_codes": len(distinct_codes),
            "review_needed_count": review_count,
            "categories_breakdown": dict(cat_counts.most_common(10))
        }

        # Apply Filters
        filtered = all_rules
        if category:
            cat_lower = category.lower()
            filtered = [r for r in filtered if cat_lower in str(r.get("Rule_Category", "")).lower()]
        if audit_action:
            action_lower = audit_action.lower()
            filtered = [r for r in filtered if action_lower == str(r.get("Audit_Action", "")).lower()]
        if lob:
            lob_lower = lob.lower()
            filtered = [r for r in filtered if lob_lower in str(r.get("Line_of_Business", "")).lower()]
        if review_flag == "yes":
            filtered = [r for r in filtered if str(r.get("Review_Flag") or "").strip() not in ("", "none", "None")]

        if search:
            q = search.lower()
            filtered = [
                r for r in filtered
                if q in str(r.get("Rule_ID", "")).lower()
                or q in str(r.get("Policy_Title", "")).lower()
                or q in str(r.get("Target_Codes", "")).lower()
                or q in str(r.get("Policy_Rule_Text", "")).lower()
                or q in str(r.get("Possible_icd_codes", "")).lower()
            ]

        total = len(filtered)
        start = (page - 1) * limit
        end = start + limit
        paginated_rules = filtered[start:end]

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "rules": paginated_rules,
            "kpis": kpis,
            "is_stub": False
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading rules: {str(e)}")


@app.post("/api/check-duplicate/{payer_id}")
async def api_check_duplicate(payer_id: str, file: UploadFile = File(...)):
    """Pre-check duplicate endpoint before uploading/starting job."""
    # Save temporary file
    temp_path = UPLOADS_DIR / f"temp_{uuid.uuid4().hex[:8]}_{file.filename}"
    with open(temp_path, "wb") as f:
        content = await file.read()
        f.write(content)

    res = check_duplicate(payer_id, str(temp_path), file.filename)
    try:
        os.remove(temp_path)
    except Exception:
        pass
    return res


@app.post("/api/upload/{payer_id}")
async def api_upload(
    payer_id: str,
    file: UploadFile = File(...),
    is_demo: bool = Form(False),
    force: bool = Form(False),
    replace: bool = Form(False)
):
    """Uploads PDF, triggers duplicate check & runs background pipeline."""
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    dest_path = UPLOADS_DIR / f"{job_id}_{file.filename}"

    with open(dest_path, "wb") as f:
        content = await file.read()
        f.write(content)

    file_hash = compute_sha256(str(dest_path))

    # Pre-check duplicate if not force
    if not (force or replace or is_demo):
        dup_res = check_duplicate(payer_id, str(dest_path), file.filename)
        if dup_res["is_duplicate"]:
            return {
                "job_id": job_id,
                "status": "duplicate_stopped",
                "duplicate_info": dup_res
            }

    job_data = {
        "id": job_id,
        "payer_id": payer_id,
        "filename": file.filename,
        "file_path": str(dest_path),
        "sha256": file_hash,
        "status": "running",
        "current_stage": 1,
        "progress_pct": 5,
        "is_demo": is_demo,
        "force": force,
        "replace": replace,
        "stages": [
            {"id": 1, "name": "Duplicate check", "status": "pending", "logs": []},
            {"id": 2, "name": "PDF text extraction", "status": "pending", "logs": []},
            {"id": 3, "name": "Rule identification", "status": "pending", "logs": []},
            {"id": 4, "name": "Rule generation", "status": "pending", "logs": []},
            {"id": 5, "name": "ICD-10 mapping (KB)", "status": "pending", "logs": []},
            {"id": 6, "name": "SQL query generation", "status": "pending", "logs": []},
            {"id": 7, "name": "Cross-payer comparison", "status": "pending", "logs": []},
            {"id": 8, "name": "Report saved", "status": "pending", "logs": []},
        ],
        "before_after": {},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    MEMORY_JOBS[job_id] = job_data
    save_job_db(job_data)

    def progress_callback(update: Dict[str, Any]):
        if job_id in MEMORY_JOBS:
            MEMORY_JOBS[job_id].update(update)
            save_job_db(MEMORY_JOBS[job_id])

    # Run in background thread
    import threading
    t = threading.Thread(
        target=run_pipeline,
        args=(
            job_id,
            payer_id,
            str(dest_path),
            file.filename,
            is_demo,
            force,
            replace,
            progress_callback
        ),
        daemon=True
    )
    t.start()

    return {"job_id": job_id, "status": "running"}


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    """Poll job status."""
    if job_id in MEMORY_JOBS:
        return MEMORY_JOBS[job_id]
    db_job = get_job_db(job_id)
    if db_job:
        MEMORY_JOBS[job_id] = db_job
        return db_job
    raise HTTPException(status_code=404, detail="Job not found")


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_status(job_id: str):
    """SSE endpoint streaming live progress to frontend."""
    async def event_generator():
        while True:
            job = MEMORY_JOBS.get(job_id) or get_job_db(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            data = json.dumps(job)
            yield f"data: {data}\n\n"

            if job.get("status") in ("completed", "failed", "duplicate_stopped"):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class ClaimQueryRequest(BaseModel):
    sql_query: str

@app.post("/api/run-sql")
def run_sql(req: ClaimQueryRequest):
    """Executes rule SQL query on in-memory DuckDB synthetic claims."""
    tester = get_claim_tester()
    res = tester.run_query(req.sql_query)
    return res


@app.get("/api/comparison")
def get_comparison(
    client1: str = "UnitedHealthcare",
    client2: Optional[str] = None,
    policy: Optional[str] = "all",
    search: Optional[str] = None,
    result_type: Optional[str] = None,
    gap_severity: Optional[str] = None
):
    """
    Reads directly from comparison_report.xlsx.
    Loads instantly without recomputation.
    Returns:
      1) UNIQUE RULES (from Unmatched_Rules where Client = client1)
      2) SIMILAR RULES (from Rule_Comparison where Client1 = client1)
      3) RULE GAPS (Unmatched from client2 + POSSIBLE_CONFLICT / DIFFERENT_DETAILS)
      4) CONFLICT SPOTLIGHT (top 3 POSSIBLE_CONFLICT)
    """
    comp_path = PROJECT_ROOT / "Rule Comparison" / "comparison_report.xlsx"
    if not comp_path.exists():
        raise HTTPException(status_code=404, detail="Comparison report comparison_report.xlsx not found.")

    try:
        wb = openpyxl.load_workbook(comp_path, read_only=True)

        # 1. Read Rule_Comparison
        ws_comp = wb["Rule_Comparison"]
        comp_rows = list(ws_comp.iter_rows(values_only=True))
        comp_h = [str(h).strip() if h else "" for h in comp_rows[0]]
        raw_similar = [dict(zip(comp_h, r)) for r in comp_rows[1:]]

        # 2. Read Unmatched_Rules
        ws_unm = wb["Unmatched_Rules"]
        unm_rows = list(ws_unm.iter_rows(values_only=True))
        unm_h = [str(h).strip() if h else "" for h in unm_rows[0]]
        raw_unmatched = [dict(zip(unm_h, r)) for r in unm_rows[1:]]

        # Filter by client1
        # Match client1 flexible ("UnitedHealthcare" or "uhc" or "Humana" or "humana")
        c1_clean = "UnitedHealthcare" if "uhc" in client1.lower() or "united" in client1.lower() else "Humana"
        c2_clean = "Humana" if c1_clean == "UnitedHealthcare" else "UnitedHealthcare"
        if client2:
            if "uhc" in client2.lower() or "united" in client2.lower():
                c2_clean = "UnitedHealthcare"
            elif "humana" in client2.lower():
                c2_clean = "Humana"

        # Unique rules for client1
        unique_rules = [r for r in raw_unmatched if str(r.get("Client")).strip().lower() == c1_clean.lower()]

        # Similar rules for client1 vs client2
        similar_rules = [
            r for r in raw_similar
            if str(r.get("Client1")).strip().lower() == c1_clean.lower()
            and (not c2_clean or str(r.get("Client2")).strip().lower() == c2_clean.lower())
        ]

        # Rule Gaps:
        # (a) other payer rules missing in client1 (Unmatched_Rules where Client = c2_clean and Not_Matched_In = c1_clean)
        # (b) pairs with POSSIBLE_CONFLICT or DIFFERENT_DETAILS
        other_unmatched_as_gaps = [
            {
                "gap_type": "MISSING_RULE",
                "Rule_ID": r.get("Rule_ID"),
                "Policy_Title": r.get("Policy_Title"),
                "Rule_Category": r.get("Rule_Category"),
                "Audit_Action": r.get("Audit_Action"),
                "Target_Codes": r.get("Target_Codes"),
                "Gap_Severity": r.get("Gap_Severity") or "HIGH",
                "Reason": r.get("Unmatched_Reason") or f"{c2_clean} has this rule, not found in {c1_clean}",
                "Policy_Rule_Text": r.get("Policy_Rule_Text")
            }
            for r in raw_unmatched
            if str(r.get("Client")).strip().lower() == c2_clean.lower()
        ]

        divergent_pairs_as_gaps = [
            {
                "gap_type": "CONFLICT_OR_DIFF",
                "Rule_ID": f"{r.get('Client1_Rule_ID')} vs {r.get('Client2_Rule_ID')}",
                "Policy_Title": f"{r.get('Client1_Policy_Title')} / {r.get('Client2_Policy_Title')}",
                "Rule_Category": f"{r.get('Client1_Category')} vs {r.get('Client2_Category')}",
                "Audit_Action": f"{r.get('Client1_Audit_Action')} vs {r.get('Client2_Audit_Action')}",
                "Target_Codes": r.get("Shared_Codes"),
                "Gap_Severity": "HIGH" if r.get("Comparison_Result") == "POSSIBLE_CONFLICT" else "MEDIUM",
                "Reason": r.get("Differences") or r.get("Comparison_Result"),
                "Policy_Rule_Text": r.get("Client1_Rule_Text")
            }
            for r in similar_rules
            if r.get("Comparison_Result") in ("POSSIBLE_CONFLICT", "DIFFERENT_DETAILS")
        ]

        all_gaps = other_unmatched_as_gaps + divergent_pairs_as_gaps
        # Sort gaps by severity (HIGH, then MEDIUM, then LOW)
        severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        all_gaps.sort(key=lambda x: severity_rank.get(str(x.get("Gap_Severity")).upper(), 3))

        # Conflict spotlight: top 3 POSSIBLE_CONFLICT pairs
        conflicts = [r for r in similar_rules if r.get("Comparison_Result") == "POSSIBLE_CONFLICT"]
        spotlight = []
        for c in conflicts[:3]:
            # Generate one-sentence summary
            shared = c.get("Shared_Codes") or "codes"
            diff = c.get("Differences") or "Conflicting coverage outcomes"
            a1 = c.get("Client1_Audit_Action", "Unknown")
            a2 = c.get("Client2_Audit_Action", "Unknown")
            summary = f"Code(s) {shared}: {c1_clean} sets outcome to {a1}, whereas {c2_clean} specifies {a2}."
            spotlight.append({
                "rule_pair": f"{c.get('Client1_Rule_ID')} vs {c.get('Client2_Rule_ID')}",
                "policies": f"{c.get('Client1_Policy_Title')} vs {c.get('Client2_Policy_Title')}",
                "shared_codes": shared,
                "summary": summary,
                "raw_diff": diff,
                "detail": c
            })

        # Apply policy filter if specific
        if policy and policy.lower() != "all":
            p_lower = policy.lower()
            unique_rules = [r for r in unique_rules if p_lower in str(r.get("Policy_Title", "")).lower()]
            similar_rules = [r for r in similar_rules if p_lower in str(r.get("Client1_Policy_Title", "")).lower()]
            all_gaps = [r for r in all_gaps if p_lower in str(r.get("Policy_Title", "")).lower()]

        # Apply search filter
        if search:
            q = search.lower()
            unique_rules = [r for r in unique_rules if q in str(r).lower()]
            similar_rules = [r for r in similar_rules if q in str(r).lower()]
            all_gaps = [r for r in all_gaps if q in str(r).lower()]

        if result_type:
            similar_rules = [r for r in similar_rules if r.get("Comparison_Result") == result_type]

        if gap_severity:
            all_gaps = [r for r in all_gaps if str(r.get("Gap_Severity")).upper() == gap_severity.upper()]

        headline = f"{len(unique_rules):,} unique rules, {len(similar_rules):,} similar rules, {len(all_gaps):,} gaps vs {c2_clean}"

        return {
            "headline": headline,
            "client1": c1_clean,
            "client2": c2_clean,
            "unique_count": len(unique_rules),
            "similar_count": len(similar_rules),
            "gaps_count": len(all_gaps),
            "unique_rules": unique_rules[:200],
            "similar_rules": similar_rules[:200],
            "rule_gaps": all_gaps[:200],
            "conflict_spotlight": spotlight
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading comparison report: {str(e)}")


@app.get("/api/demo-samples")
def get_demo_samples():
    """Returns pre-loaded sample policies for demo mode."""
    if DEMO_SAMPLES_PATH.exists():
        with open(DEMO_SAMPLES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


@app.get("/api/download/workbook/{payer_id}")
def download_workbook(payer_id: str):
    wb_rel = "Uhc/billing_rules_uhc.xlsx" if payer_id == "uhc" else "Humana/billing_rules_humana.xlsx" if payer_id == "humana" else None
    if not wb_rel:
        raise HTTPException(status_code=404, detail="No workbook available for this payer.")
    p = PROJECT_ROOT / wb_rel
    if not p.exists():
        raise HTTPException(status_code=404, detail="Workbook file not found on disk.")
    return FileResponse(
        path=str(p),
        filename=p.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.get("/api/download/queries/{payer_id}")
def download_queries(payer_id: str):
    sql_rel = "Uhc/billing_rules_uhc.sql" if payer_id == "uhc" else "Humana/billing_rules_humana.sql" if payer_id == "humana" else None
    if not sql_rel:
        raise HTTPException(status_code=404, detail="No SQL queries available for this payer.")
    p = PROJECT_ROOT / sql_rel
    if not p.exists():
        # Look for claim_table_skeleton or generate on the fly
        p = PROJECT_ROOT / payer_id / "claim_table_skeleton.sql"
    if not p.exists():
        raise HTTPException(status_code=404, detail="SQL queries file not found.")
    return FileResponse(
        path=str(p),
        filename=p.name,
        media_type="text/plain"
    )


# Serve frontend index.html
FRONTEND_DIR = DEMO_APP_DIR / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Frontend index.html is being built...</h1>")
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)

