"""
app.py
======
FastAPI backend for the Payer Policy Billing Rule Pipeline Demo.
Serves the 3-page Single Page Application, handles real subprocess pipeline runs,
returns rule details, runs cross-payer rule comparison, and serves downloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import openpyxl
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pipeline_demo")

# Base directories
BACKEND_DIR = Path(__file__).resolve().parent
DEMO_DIR = BACKEND_DIR.parent
FRONTEND_DIR = DEMO_DIR / "frontend"
PODS_DIR = DEMO_DIR / "pods"
DATA_DIR = DEMO_DIR / "data"
BASELINE_DIR = DATA_DIR / "baseline"
RUNS_DIR = DATA_DIR / "runs"
COMPARISON_DIR = DEMO_DIR / "comparison"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BASELINE_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# Pod registry configuration
PODS: Dict[str, Dict[str, Any]] = {
    "uhc": {
        "id": "uhc",
        "display_name": "United Healthcare",
        "client_key": "uhc",
        "payer_name": "UnitedHealthcare",
        "dir": PODS_DIR / "uhc",
        "extraction_script": "extraction_script.py",
        "generator_script": "generate_billing_rules_uhc.py",
        "baseline_file": BASELINE_DIR / "billing_rules_uhc.xlsx",
        "generator_extra_args": ["--lob", "all"],
    },
    "humana": {
        "id": "humana",
        "display_name": "Humana",
        "client_key": "humana",
        "payer_name": "Humana",
        "dir": PODS_DIR / "humana",
        "extraction_script": "extraction_script.py",
        "generator_script": "generate_billing_rules_humana.py",
        "baseline_file": BASELINE_DIR / "billing_rules_humana.xlsx",
        "generator_extra_args": [],
    },
    "devoted": {
        "id": "devoted",
        "display_name": "Devoted Health",
        "client_key": "devoted",
        "payer_name": "Devoted Health",
        "dir": PODS_DIR / "devoted",
        "extraction_script": "extraction_script.py",
        "generator_script": "generate_billing_rules_devoted.py",
        "baseline_file": BASELINE_DIR / "billing_rules_devoted.xlsx",
        "generator_extra_args": [],
    },
}

CPT_REGEX = re.compile(r"^(?:\d{4}[0-9A-Z]|[A-V]\d{4})$")

# In-memory run state tracking with thread lock
RUN_LOCK = threading.Lock()
RUNS_STATE: Dict[str, Dict[str, Any]] = {}


def expand_target_codes(text: Any) -> Set[str]:
    """Expands comma-separated codes and ranges like '93224-93227, 99291, A0428'."""
    codes: Set[str] = set()
    if not text:
        return codes
    for part in re.split(r"[,;\n]", str(text)):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([A-Z]?)(\d{4,5})([A-Z]?)\s*[-–]\s*([A-Z]?)(\d{4,5})([A-Z]?)", part)
        if m and m.group(1) == m.group(4) and m.group(3) == m.group(6):
            pfx = m.group(1)
            sfx = m.group(3)
            try:
                start_n, end_n = int(m.group(2)), int(m.group(5))
                if 0 <= end_n - start_n <= 2000:
                    width = len(m.group(2))
                    for num in range(start_n, end_n + 1):
                        c = f"{pfx}{str(num).zfill(width)}{sfx}"
                        if CPT_REGEX.fullmatch(c):
                            codes.add(c)
                    continue
            except Exception:
                pass
        for token in re.findall(r"\b(?:\d{4}[0-9A-Z]|[A-V]\d{4})\b", part):
            if CPT_REGEX.fullmatch(token):
                codes.add(token)
    return codes


def sanitize_filename(filename: str) -> str:
    """Sanitizes filename against directory traversal and dangerous characters."""
    base = Path(filename).name
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]", "_", base)
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


# Cache for summary KPIs
_SUMMARY_CACHE: Dict[str, Any] = {"mtimes": {}, "data": None}


def compute_summary() -> Dict[str, Any]:
    """Computes Home page KPIs and per-pod rule/CPT counts from baseline workbooks."""
    current_mtimes = {}
    for pid, pinfo in PODS.items():
        bf = pinfo["baseline_file"]
        current_mtimes[pid] = bf.stat().st_mtime if bf.exists() else 0

    if _SUMMARY_CACHE["data"] and _SUMMARY_CACHE["mtimes"] == current_mtimes:
        return _SUMMARY_CACHE["data"]

    all_cpt_codes: Set[str] = set()
    total_rules = 0
    pods_summary = []

    for pid in ["uhc", "humana", "devoted"]:
        pinfo = PODS[pid]
        bf = pinfo["baseline_file"]
        pod_rules = 0
        pod_cpt_codes: Set[str] = set()

        if bf.exists():
            try:
                wb = openpyxl.load_workbook(bf, read_only=True)
                if "Rule_Detail" in wb.sheetnames:
                    ws = wb["Rule_Detail"]
                    rows = list(ws.iter_rows(values_only=True))
                    if len(rows) > 1:
                        headers = list(rows[0])
                        tc_idx = headers.index("Target_Codes") if "Target_Codes" in headers else -1
                        pod_rules = len(rows) - 1
                        for r in rows[1:]:
                            tc_val = r[tc_idx] if tc_idx >= 0 and len(r) > tc_idx else ""
                            c = expand_target_codes(tc_val)
                            pod_cpt_codes |= c
                            all_cpt_codes |= c
                wb.close()
            except Exception as e:
                logger.error("Error reading baseline workbook %s: %s", bf, e)

        total_rules += pod_rules
        pods_summary.append({
            "id": pid,
            "name": pinfo["display_name"],
            "rules_count": pod_rules,
            "cpt_count": len(pod_cpt_codes),
        })

    summary_data = {
        "total_rules": total_rules,
        "total_cpt_codes": len(all_cpt_codes),
        "total_pods": len(PODS),
        "pods": pods_summary,
    }

    _SUMMARY_CACHE["mtimes"] = current_mtimes
    _SUMMARY_CACHE["data"] = summary_data
    return summary_data


def save_run_state(run_id: str) -> None:
    """Persists run state to data/runs/<run_id>/run_state.json."""
    with RUN_LOCK:
        state = RUNS_STATE.get(run_id)
        if not state:
            return
        state_copy = json.loads(json.dumps(state))

    run_dir = RUNS_DIR / run_id
    if run_dir.exists():
        state_file = run_dir / "run_state.json"
        tmp_file = run_dir / "run_state.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state_copy, f, indent=2)
            os.replace(tmp_file, state_file)
        except Exception as e:
            logger.error("Error saving run state for %s: %s", run_id, e)


def execute_pipeline(run_id: str, pod_id: str, pdf_filename: str) -> None:
    """Background worker executing extraction and rule generation in real subprocesses."""
    pod_cfg = PODS[pod_id]
    run_dir = RUNS_DIR / run_id
    input_dir = run_dir / "input"
    extracted_dir = run_dir / "extracted"
    master_json_path = run_dir / "extraction.json"
    client_key = pod_cfg["client_key"]
    output_wb_name = f"billing_rules_{client_key}.xlsx"
    output_wb_path = run_dir / output_wb_name

    logger.info("Starting pipeline execution for run %s (pod: %s)", run_id, pod_id)

    # -------------------------------------------------------------------------
    # STAGE 2: EXTRACTION
    # -------------------------------------------------------------------------
    with RUN_LOCK:
        RUNS_STATE[run_id]["steps"][1]["status"] = "running"
        RUNS_STATE[run_id]["steps"][1]["start_time"] = time.time()
    save_run_state(run_id)

    ext_script = pod_cfg["dir"] / pod_cfg["extraction_script"]
    if pod_id == "humana":
        cmd_ext = [
            sys.executable,
            str(ext_script),
            "--input-dir", str(input_dir),
            "--output-dir", str(extracted_dir),
            "--master-json", str(master_json_path),
            "--force",
        ]
    else:
        cmd_ext = [
            sys.executable,
            str(ext_script),
            "--input-dir", str(input_dir),
            "--output-dir", str(extracted_dir),
            "--master-json", str(master_json_path),
        ]

    logger.info("Executing extraction: %s", " ".join(cmd_ext))
    proc_ext = subprocess.Popen(
        cmd_ext,
        cwd=str(pod_cfg["dir"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    log_lines: List[str] = []
    if proc_ext.stdout:
        for line in proc_ext.stdout:
            line_str = line.rstrip()
            log_lines.append(line_str)
            if len(log_lines) > 200:
                log_lines.pop(0)
            with RUN_LOCK:
                RUNS_STATE[run_id]["steps"][1]["log"] = log_lines[-50:]

    proc_ext.wait(timeout=600)
    ext_elapsed = round(time.time() - RUNS_STATE[run_id]["steps"][1]["start_time"], 1)

    if proc_ext.returncode != 0 or not master_json_path.exists():
        err_msg = f"Extraction failed with exit code {proc_ext.returncode}"
        logger.error("Run %s: %s", run_id, err_msg)
        with RUN_LOCK:
            RUNS_STATE[run_id]["status"] = "failed"
            RUNS_STATE[run_id]["error"] = err_msg
            RUNS_STATE[run_id]["steps"][1]["status"] = "failed"
            RUNS_STATE[run_id]["steps"][1]["elapsed"] = ext_elapsed
            RUNS_STATE[run_id]["steps"][1]["log"] = log_lines[-50:]
        save_run_state(run_id)
        return

    # Compute extraction metrics
    policies_count = 0
    sections_count = 0
    try:
        with open(master_json_path, "r", encoding="utf-8") as f:
            ext_data = json.load(f)
            if isinstance(ext_data, list):
                policies_count = len(ext_data)
                for p in ext_data:
                    if isinstance(p, dict):
                        sections_count += len(p.get("sections", []) or p.get("extracted_content", []) or [])
            elif isinstance(ext_data, dict):
                policies_count = 1
                sections_count = len(ext_data.get("sections", []) or ext_data.get("extracted_content", []) or [])
    except Exception as e:
        logger.warning("Error parsing extraction.json for metrics: %s", e)
        policies_count = 1

    with RUN_LOCK:
        RUNS_STATE[run_id]["steps"][1]["status"] = "done"
        RUNS_STATE[run_id]["steps"][1]["elapsed"] = ext_elapsed
        RUNS_STATE[run_id]["steps"][1]["details"] = (
            f"{policies_count} {'policy' if policies_count == 1 else 'policies'}, "
            f"{sections_count} sections extracted"
        )
        RUNS_STATE[run_id]["steps"][1]["log"] = log_lines[-50:]
        RUNS_STATE[run_id]["metrics"]["policies_extracted"] = policies_count
        RUNS_STATE[run_id]["metrics"]["sections_extracted"] = sections_count
        RUNS_STATE[run_id]["outputs"]["extraction_json"] = "extraction.json"
    save_run_state(run_id)

    # -------------------------------------------------------------------------
    # STAGE 3: RULE GENERATION
    # -------------------------------------------------------------------------
    with RUN_LOCK:
        RUNS_STATE[run_id]["steps"][2]["status"] = "running"
        RUNS_STATE[run_id]["steps"][2]["start_time"] = time.time()
    save_run_state(run_id)

    gen_script = pod_cfg["dir"] / pod_cfg["generator_script"]
    cmd_gen = [
        sys.executable,
        str(gen_script),
        "--input", str(master_json_path),
        "--out-dir", str(run_dir),
        "--compare-dir", "",
    ] + pod_cfg["generator_extra_args"]

    logger.info("Executing rule generation: %s", " ".join(cmd_gen))
    proc_gen = subprocess.Popen(
        cmd_gen,
        cwd=str(pod_cfg["dir"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    log_lines_gen: List[str] = []
    if proc_gen.stdout:
        for line in proc_gen.stdout:
            line_str = line.rstrip()
            log_lines_gen.append(line_str)
            if len(log_lines_gen) > 200:
                log_lines_gen.pop(0)
            with RUN_LOCK:
                RUNS_STATE[run_id]["steps"][2]["log"] = log_lines_gen[-50:]

    proc_gen.wait(timeout=600)
    gen_elapsed = round(time.time() - RUNS_STATE[run_id]["steps"][2]["start_time"], 1)

    if proc_gen.returncode != 0 or not output_wb_path.exists():
        err_msg = f"Rule generation failed with exit code {proc_gen.returncode}"
        logger.error("Run %s: %s", run_id, err_msg)
        with RUN_LOCK:
            RUNS_STATE[run_id]["status"] = "failed"
            RUNS_STATE[run_id]["error"] = err_msg
            RUNS_STATE[run_id]["steps"][2]["status"] = "failed"
            RUNS_STATE[run_id]["steps"][2]["elapsed"] = gen_elapsed
            RUNS_STATE[run_id]["steps"][2]["log"] = log_lines_gen[-50:]
        save_run_state(run_id)
        return

    # Count generated rules from the workbook
    rules_count = 0
    try:
        wb = openpyxl.load_workbook(output_wb_path, read_only=True)
        if "Rule_Detail" in wb.sheetnames:
            ws = wb["Rule_Detail"]
            rules_count = max(0, len(list(ws.iter_rows(values_only=True))) - 1)
        wb.close()
    except Exception as e:
        logger.warning("Error reading generated workbook %s: %s", output_wb_path, e)

    with RUN_LOCK:
        RUNS_STATE[run_id]["status"] = "done"
        RUNS_STATE[run_id]["steps"][2]["status"] = "done"
        RUNS_STATE[run_id]["steps"][2]["elapsed"] = gen_elapsed
        RUNS_STATE[run_id]["steps"][2]["details"] = f"{rules_count} rules generated"
        RUNS_STATE[run_id]["steps"][2]["log"] = log_lines_gen[-50:]
        RUNS_STATE[run_id]["metrics"]["rules_generated"] = rules_count
        RUNS_STATE[run_id]["outputs"]["rules_workbook"] = output_wb_name
    save_run_state(run_id)
    logger.info("Pipeline completed successfully for run %s: %d rules generated", run_id, rules_count)


# =============================================================================
# FastAPI Application & Endpoints
# =============================================================================

app = FastAPI(title="Payer Policy Billing Rule Pipeline Demo", version="1.0.0")

# Localhost only CORS (no wildcard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/summary")
def get_summary() -> Dict[str, Any]:
    """Returns baseline KPIs and per-pod summary counts."""
    return compute_summary()


@app.post("/api/pods/{pod_id}/runs")
async def start_run(pod_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    """Uploads a policy PDF for a pod and starts the background execution."""
    if pod_id not in PODS:
        raise HTTPException(status_code=400, detail=f"Invalid pod ID '{pod_id}'. Expected one of: {list(PODS.keys())}")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    safe_name = sanitize_filename(file.filename)

    # Read header and check magic bytes %PDF
    content = await file.read()
    if len(content) < 4 or not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Invalid file: uploaded file is not a valid PDF (%PDF header missing).")

    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size exceeds maximum allowed limit (25 MB).")

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    run_dir = RUNS_DIR / run_id
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    dest_file = input_dir / safe_name
    with open(dest_file, "wb") as f:
        f.write(content)

    filesize_kb = round(len(content) / 1024, 1)

    initial_state: Dict[str, Any] = {
        "run_id": run_id,
        "pod_id": pod_id,
        "filename": safe_name,
        "filesize_kb": filesize_kb,
        "created_at": time.time(),
        "status": "running",
        "error": None,
        "steps": [
            {
                "name": "Upload saved",
                "status": "done",
                "elapsed": 0.1,
                "details": f"{safe_name} ({filesize_kb} KB)",
                "log": [f"File {safe_name} verified and saved ({filesize_kb} KB)."],
            },
            {
                "name": "Extraction",
                "status": "waiting",
                "elapsed": 0.0,
                "details": "",
                "log": [],
            },
            {
                "name": "Rule generation",
                "status": "waiting",
                "elapsed": 0.0,
                "details": "",
                "log": [],
            },
        ],
        "metrics": {
            "policies_extracted": 0,
            "sections_extracted": 0,
            "rules_generated": 0,
        },
        "outputs": {
            "extraction_json": None,
            "rules_workbook": None,
            "comparison_report": None,
        },
    }

    with RUN_LOCK:
        RUNS_STATE[run_id] = initial_state
    save_run_state(run_id)

    # Launch background worker
    t = threading.Thread(target=execute_pipeline, args=(run_id, pod_id, safe_name), daemon=True)
    t.start()

    return {"run_id": run_id, "pod_id": pod_id, "filename": safe_name}


@app.get("/api/runs/{run_id}")
def get_run_status(run_id: str) -> Dict[str, Any]:
    """Returns current status, step progression, log tails, and outputs for a run."""
    with RUN_LOCK:
        if run_id in RUNS_STATE:
            return RUNS_STATE[run_id]

    state_file = RUNS_DIR / run_id / "run_state.json"
    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                with RUN_LOCK:
                    RUNS_STATE[run_id] = data
                return data
        except Exception:
            pass

    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")


@app.get("/api/runs/{run_id}/rules")
def get_run_rules(run_id: str) -> Dict[str, Any]:
    """Returns generated rules parsed from the run's Rule_Detail sheet."""
    run_state = get_run_status(run_id)
    pod_id = run_state["pod_id"]
    client_key = PODS[pod_id]["client_key"]
    wb_path = RUNS_DIR / run_id / f"billing_rules_{client_key}.xlsx"

    if not wb_path.exists():
        raise HTTPException(status_code=404, detail="Billing rules workbook has not been generated for this run.")

    rules: List[Dict[str, Any]] = []
    categories: Set[str] = set()

    try:
        wb = openpyxl.load_workbook(wb_path, read_only=True)
        if "Rule_Detail" not in wb.sheetnames:
            wb.close()
            raise HTTPException(status_code=500, detail="Sheet 'Rule_Detail' missing in generated workbook.")

        ws = wb["Rule_Detail"]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        if len(rows) > 1:
            headers = [str(h or "") for h in rows[0]]
            col_map = {name: idx for idx, name in enumerate(headers)}

            for r in rows[1:]:
                def get_c(col_name: str) -> str:
                    idx = col_map.get(col_name, -1)
                    if idx >= 0 and idx < len(r):
                        v = r[idx]
                        return str(v).strip() if v is not None else ""
                    return ""

                rid = get_c("Rule_ID")
                if not rid:
                    continue

                cat = get_c("Rule_Category")
                if cat:
                    categories.add(cat)

                rules.append({
                    "rule_id": rid,
                    "payer": get_c("Payer"),
                    "policy_title": get_c("Policy_Title"),
                    "policy_number": get_c("Policy_Number"),
                    "lob": get_c("Line_of_Business"),
                    "rule_category": cat,
                    "target_codes": get_c("Target_Codes"),
                    "possible_icd_codes": get_c("Possible_icd_codes"),
                    "target_modifiers": get_c("Target_Modifiers"),
                    "audit_action": get_c("Audit_Action"),
                    "rule_text": get_c("Policy_Rule_Text"),
                    "exception_text": get_c("Exception_Text"),
                    "sql_query": get_c("SQL_Query"),
                    "source_pdf": get_c("Source_PDF"),
                    "source_page": get_c("Source_Page"),
                })
    except Exception as e:
        logger.error("Error reading rules from %s: %s", wb_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to read rules workbook: {e}")

    return {
        "run_id": run_id,
        "pod_id": pod_id,
        "total": len(rules),
        "categories": sorted(list(categories)),
        "rules": rules,
    }


@app.post("/api/runs/{run_id}/compare")
def run_comparison(run_id: str) -> Dict[str, Any]:
    """Runs cross-payer rule comparison against the baseline workbooks of the other 2 pods."""
    run_state = get_run_status(run_id)
    current_pod_id = run_state["pod_id"]
    client_key = PODS[current_pod_id]["client_key"]
    current_payer_name = PODS[current_pod_id]["payer_name"]

    current_wb = RUNS_DIR / run_id / f"billing_rules_{client_key}.xlsx"
    if not current_wb.exists():
        raise HTTPException(status_code=400, detail="Cannot run comparison: run workbook does not exist.")

    # Find the other two pods
    other_pod_ids = [p for p in PODS.keys() if p != current_pod_id]
    other_wbs = [PODS[p]["baseline_file"] for p in other_pod_ids]

    for owb in other_wbs:
        if not owb.exists():
            raise HTTPException(status_code=500, detail=f"Baseline workbook missing: {owb.name}. Run build_baseline.py first.")

    out_report_path = RUNS_DIR / run_id / "comparison_report.xlsx"
    compare_script = COMPARISON_DIR / "compare_rules.py"

    cmd = [
        sys.executable,
        str(compare_script),
        "--inputs",
        str(current_wb),
        str(other_wbs[0]),
        str(other_wbs[1]),
        "--primary",
        current_payer_name,
        "--out",
        str(out_report_path),
    ]

    logger.info("Running comparison for %s: %s", run_id, " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(COMPARISON_DIR), capture_output=True, text=True, encoding="utf-8")

    if res.returncode != 0 or not out_report_path.exists():
        logger.error("Comparison failed: %s", res.stderr or res.stdout)
        raise HTTPException(status_code=500, detail=f"Comparison failed: {res.stderr or res.stdout}")

    with RUN_LOCK:
        if run_id in RUNS_STATE:
            RUNS_STATE[run_id]["outputs"]["comparison_report"] = "comparison_report.xlsx"
    save_run_state(run_id)

    # Return comparison data directly
    return get_comparison_data(run_id)


@app.get("/api/runs/{run_id}/compare")
def get_comparison_data(run_id: str) -> Dict[str, Any]:
    """Returns the comparison matrix and unmatched rules for this run, filtered to Client1 = current pod."""
    run_state = get_run_status(run_id)
    pod_id = run_state["pod_id"]
    current_payer_name = PODS[pod_id]["payer_name"]

    report_path = RUNS_DIR / run_id / "comparison_report.xlsx"
    if not report_path.exists():
        # Auto-run comparison if not already executed
        return run_comparison(run_id)

    matched_pairs: List[Dict[str, Any]] = []
    unmatched_rules: List[Dict[str, Any]] = []

    matched_count = 0
    differing_count = 0
    conflicting_count = 0
    unmatched_count = 0

    try:
        wb = openpyxl.load_workbook(report_path, read_only=True)

        # 1. Parse Rule_Comparison
        if "Rule_Comparison" in wb.sheetnames:
            ws = wb["Rule_Comparison"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 1:
                headers = [str(h or "") for h in rows[0]]
                col_map = {name: idx for idx, name in enumerate(headers)}

                for r in rows[1:]:
                    def get_c(col_name: str) -> str:
                        idx = col_map.get(col_name, -1)
                        if idx >= 0 and idx < len(r):
                            v = r[idx]
                            return str(v).strip() if v is not None else ""
                        return ""

                    c1 = get_c("Client1")
                    c2 = get_c("Client2")

                    # Strictly compare THIS client to the other 2 clients; eliminate self-comparison
                    if c1.lower() != current_payer_name.lower():
                        continue
                    if c2.lower() == current_payer_name.lower() or c1.lower() == c2.lower():
                        continue

                    res_type = get_c("Comparison_Result")
                    if res_type == "SAME_RULE":
                        matched_count += 1
                    elif res_type in ("DIFFERENT_DETAILS", "SAME_CODES_DIFFERENT_RULE_DETAILS"):
                        differing_count += 1
                    elif res_type == "POSSIBLE_CONFLICT":
                        conflicting_count += 1

                    matched_pairs.append({
                        "client1": c1,
                        "client2": get_c("Client2"),
                        "result": res_type,
                        "shared_code_count": get_c("Shared_Code_Count"),
                        "shared_codes": get_c("Shared_Codes"),
                        "differences": get_c("Differences"),
                        "c1_rule_id": get_c("Client1_Rule_ID"),
                        "c1_policy_title": get_c("Client1_Policy_Title"),
                        "c1_category": get_c("Client1_Category"),
                        "c1_rule_text": get_c("Client1_Rule_Text"),
                        "c2_rule_id": get_c("Client2_Rule_ID"),
                        "c2_policy_title": get_c("Client2_Policy_Title"),
                        "c2_category": get_c("Client2_Category"),
                        "c2_rule_text": get_c("Client2_Rule_Text"),
                    })

        # 2. Parse Unmatched_Rules
        if "Unmatched_Rules" in wb.sheetnames:
            ws = wb["Unmatched_Rules"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 1:
                headers = [str(h or "") for h in rows[0]]
                col_map = {name: idx for idx, name in enumerate(headers)}

                for r in rows[1:]:
                    def get_u(col_name: str) -> str:
                        idx = col_map.get(col_name, -1)
                        if idx >= 0 and idx < len(r):
                            v = r[idx]
                            return str(v).strip() if v is not None else ""
                        return ""

                    c = get_u("Client")
                    not_matched_in = get_u("Not_Matched_In")

                    # Strictly for this client missing in the other 2 clients; eliminate self-comparison
                    if c.lower() != current_payer_name.lower():
                        continue
                    if not_matched_in.lower() == current_payer_name.lower():
                        continue

                    unmatched_count += 1
                    unmatched_rules.append({
                        "client": c,
                        "not_matched_in": get_u("Not_Matched_In"),
                        "gap_severity": get_u("Gap_Severity"),
                        "unmatched_reason": get_u("Unmatched_Reason"),
                        "rule_id": get_u("Rule_ID"),
                        "policy_title": get_u("Policy_Title"),
                        "rule_category": get_u("Rule_Category"),
                        "target_codes": get_u("Target_Codes"),
                        "rule_text": get_u("Policy_Rule_Text"),
                    })

        wb.close()
    except Exception as e:
        logger.error("Error reading comparison report %s: %s", report_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to read comparison report: {e}")

    # Build Left-Right grouped structure for the UI
    # We want: for each rule of current policy -> matches grouped by other pod
    # Also collect unmatched reasons per other pod
    rules_dict: Dict[str, Dict[str, Any]] = {}

    # Get this run's rules as base list
    rules_resp = get_run_rules(run_id)
    for r in rules_resp["rules"]:
        rid = r["rule_id"]
        rules_dict[rid] = {
            "rule_id": rid,
            "policy_title": r["policy_title"],
            "rule_category": r["rule_category"],
            "rule_text": r["rule_text"],
            "target_codes": r["target_codes"],
            "matches_by_pod": {},
            "unmatched_by_pod": {},
            "match_count_total": 0,
        }

    # Populate matches
    for pair in matched_pairs:
        rid = pair["c1_rule_id"]
        c2 = pair["client2"]
        if rid in rules_dict:
            rules_dict[rid]["matches_by_pod"].setdefault(c2, []).append(pair)
            rules_dict[rid]["match_count_total"] += 1

    # Populate unmatched reasons
    for un in unmatched_rules:
        rid = un["rule_id"]
        other_pod = un["not_matched_in"]
        if rid in rules_dict:
            rules_dict[rid]["unmatched_by_pod"][other_pod] = un["unmatched_reason"]

    other_pod_names = [p["payer_name"] for pid, p in PODS.items() if pid != pod_id]

    return {
        "run_id": run_id,
        "pod_id": pod_id,
        "current_payer": current_payer_name,
        "other_payers": other_pod_names,
        "summary": {
            "matched": matched_count,
            "differing": differing_count,
            "conflicting": conflicting_count,
            "unmatched": unmatched_count,
            "total_pairs": len(matched_pairs),
        },
        "report_filename": "comparison_report.xlsx",
        "rules": list(rules_dict.values()),
    }


@app.get("/api/runs/{run_id}/files/{filename:path}")
def download_run_file(run_id: str, filename: str) -> FileResponse:
    """Safe download endpoint for run artifacts (PDFs, JSON, Excel reports)."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=403, detail="Access denied: path traversal attempt detected.")

    safe_name = Path(filename).name
    target_path = (RUNS_DIR / run_id / safe_name).resolve()

    # If it's the input PDF, look inside input/
    if not target_path.exists():
        input_target = (RUNS_DIR / run_id / "input" / safe_name).resolve()
        if input_target.exists():
            target_path = input_target

    # Confirm it stays inside RUNS_DIR
    try:
        target_path.relative_to(RUNS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal attempt detected.")

    if not target_path.is_file():
        raise HTTPException(status_code=404, detail=f"File '{safe_name}' not found in run '{run_id}'.")

    media_type = "application/octet-stream"
    if safe_name.endswith(".json"):
        media_type = "application/json"
    elif safe_name.endswith(".xlsx"):
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif safe_name.endswith(".pdf"):
        media_type = "application/pdf"

    return FileResponse(path=str(target_path), filename=safe_name, media_type=media_type)


# Mount static assets
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/{rest_of_path:path}", response_class=HTMLResponse)
def serve_index(rest_of_path: str = "") -> HTMLResponse:
    """Serves the Single Page Application index.html for all view routes."""
    if rest_of_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found")
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="frontend/index.html not found")
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
