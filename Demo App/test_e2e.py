"""
test_e2e.py
===========
End-to-End Verification Test Script for Payer Policy Demo App.
Tests:
  1. API Health & Payer Registry (Pods, live counts, summary, heatmap)
  2. PDF Ingestion & 8-Stage Stepper Execution
  3. Rule Generation, SQL Queries & DuckDB Synthetic Claim Execution
  4. Duplicate Check & Banner Verification
  5. Cross-Payer Comparison (Unique, Similar, Gaps, Conflict Spotlight)
"""

import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient

DEMO_APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_APP_DIR / "backend"))
from app import app

client = TestClient(app)

def test_all():
    print("=" * 70)
    print("STEP 1: Testing Home Page Endpoints (Payers, Summary, Heatmap)")
    print("=" * 70)

    # 1.1 Payers Registry
    r_payers = client.get("/api/payers")
    assert r_payers.status_code == 200, f"Failed /api/payers: {r_payers.text}"
    payers = r_payers.json()
    print(f"Loaded {len(payers)} payers:")
    for p in payers:
        print(f"  - {p['display_name']} [{p['status']}] (Policies: {p['policies_count']}, Rules: {p['rules_count']}, Codes: {p['distinct_codes']})")
    assert any(p["id"] == "uhc" for p in payers), "UHC missing from registry"
    assert any(p["id"] == "humana" for p in payers), "Humana missing from registry"
    assert any(p["id"] == "devoted" and p["badge"] for p in payers), "Devoted stub missing or badge missing"

    # 1.2 Summary
    r_sum = client.get("/api/summary")
    assert r_sum.status_code == 200
    sum_data = r_sum.json()
    print(f"Summary Strip: Payers={sum_data['total_payers']}, Rules={sum_data['total_rules']}, Matched Pairs={sum_data['total_matched_pairs']}, Gaps={sum_data['total_gaps']}")
    assert sum_data["total_rules"] > 0, "Total rules should be > 0"
    assert sum_data["total_matched_pairs"] > 0, "Matched pairs should be > 0"

    # 1.3 Heatmap
    r_heat = client.get("/api/heatmap")
    assert r_heat.status_code == 200
    heat_data = r_heat.json()
    print(f"Heatmap Categories: {len(heat_data['categories'])} categories across {len(heat_data['payers'])} payers.")

    print("\n" + "=" * 70)
    print("STEP 2: Testing Rule Retrieval & DuckDB Claim Query Execution")
    print("=" * 70)

    # 2.1 Fetch UHC Rules
    r_rules = client.get("/api/rules/uhc?limit=5")
    assert r_rules.status_code == 200
    rules_resp = r_rules.json()
    rules = rules_resp["rules"]
    print(f"Retrieved {len(rules)} of {rules_resp['total']} UHC rules.")
    assert len(rules) > 0, "Should have UHC rules"

    first_rule = rules[0]
    print(f"First Rule ID: {first_rule.get('Rule_ID')} | Category: {first_rule.get('Rule_Category')}")
    sql = first_rule.get("SQL_Query")
    assert sql, "Rule should have a generated SQL query"

    # 2.2 Execute SQL on DuckDB synthetic claims
    r_sql = client.post("/api/run-sql", json={"sql_query": sql})
    assert r_sql.status_code == 200
    sql_res = r_sql.json()
    print(f"DuckDB Query Execution: success={sql_res['success']}, message={sql_res.get('message')}")
    assert sql_res["success"], f"DuckDB execution failed: {sql_res.get('error')}"

    print("\n" + "=" * 70)
    print("STEP 3: Testing Policy Upload & 8-Stage Pipeline Stepper")
    print("=" * 70)

    # Use a real sample PDF or simulated upload in Demo mode
    sample_pdf = DEMO_APP_DIR.parent / "Pods" / "Uhc" / "commercial_plans" / "Reimbursement" / "1012024 – UnitedHealthcare Commercial Reimbursement Policy.pdf"
    if sample_pdf.exists():
        with open(sample_pdf, "rb") as f:
            pdf_bytes = f.read()
        filename = sample_pdf.name
    else:
        pdf_bytes = b"%PDF-1.4 sample policy test"
        filename = "Hospital_Inclusive_Charges_Policy.pdf"

    # Run upload (demo mode test for fast deterministic timing verification)
    r_up = client.post(
        "/api/upload/uhc",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data={"is_demo": "true", "force": "false", "replace": "false"}
    )
    assert r_up.status_code == 200, f"Upload failed: {r_up.text}"
    job_info = r_up.json()
    job_id = job_info["job_id"]
    print(f"Uploaded policy. Job ID: {job_id} (Status: {job_info['status']})")

    # Poll until job completes
    print("Monitoring 8-Stage Pipeline Progress:")
    max_wait = 15
    start_time = time.time()
    final_job = None
    while time.time() - start_time < max_wait:
        r_poll = client.get(f"/api/jobs/{job_id}")
        assert r_poll.status_code == 200
        final_job = r_poll.json()
        stage = final_job.get("current_stage", 1)
        pct = final_job.get("progress_pct", 0)
        status = final_job.get("status", "")
        print(f"  Stage {stage}/8: {pct}% [{status}]")
        if status in ("completed", "failed"):
            break
        time.sleep(1.0)

    assert final_job["status"] == "completed", f"Job failed or did not finish: {final_job}"
    print("8-Stage Pipeline successfully completed!")
    if final_job.get("before_after"):
        ba = final_job["before_after"]
        print(f"Before/After Counter: +{ba.get('rules_diff')} rules, +{ba.get('codes_diff')} codes, +{ba.get('gaps_diff')} new gaps vs {ba.get('other_payer')}")

    print("\n" + "=" * 70)
    print("STEP 4: Testing Duplicate Detection & Banner")
    print("=" * 70)

    # Re-upload the exact same PDF in real mode -> must flag duplicate
    r_dup = client.post(
        "/api/upload/uhc",
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data={"is_demo": "false", "force": "false", "replace": "false"}
    )
    assert r_dup.status_code == 200
    dup_res = r_dup.json()
    print(f"Duplicate Re-upload Result: status={dup_res.get('status')}")
    assert dup_res.get("status") == "duplicate_stopped", f"Expected duplicate_stopped, got: {dup_res}"
    dup_info = dup_res.get("duplicate_info", {})
    print(f"Duplicate info: {dup_info.get('message')} (Rules count: {dup_info.get('existing_rules_count')})")

    print("\n" + "=" * 70)
    print("STEP 5: Testing Cross-Payer Comparison (Unique, Similar, Gaps, Conflict Spotlight)")
    print("=" * 70)

    r_comp = client.get("/api/comparison?client1=UnitedHealthcare&client2=Humana")
    assert r_comp.status_code == 200
    comp = r_comp.json()
    print(f"Headline: {comp.get('headline')}")
    print(f"  - Unique Rules: {comp.get('unique_count')}")
    print(f"  - Similar Rules: {comp.get('similar_count')}")
    print(f"  - Rule Gaps: {comp.get('gaps_count')}")

    spotlight = comp.get("conflict_spotlight", [])
    print(f"Conflict Spotlight ({len(spotlight)} high-risk conflict pairs):")
    for idx, s in enumerate(spotlight):
        print(f"  {idx+1}. [{s['rule_pair']}] {s['summary']}")

    assert comp.get("unique_count") > 0, "Should have unique rules"
    assert comp.get("similar_count") > 0, "Should have similar rules"
    assert comp.get("gaps_count") > 0, "Should have rule gaps"
    assert len(spotlight) > 0, "Should have conflict spotlight pairs"

    print("\n" + "=" * 70)
    print("ALL END-TO-END VERIFICATION CHECKS PASSED WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    test_all()

