"""
test_pipeline_demo.py
=====================
Comprehensive verification test suite testing all endpoints, real subprocess runs,
workbook counts, compare alignment, and directory isolation for pipeline_demo.
"""

import os
import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
from app import app, PODS

client = TestClient(app)

SAMPLE_PDFS = {
    "uhc": Path("Demo App/data/uploads/job-afc53512f0_Testosterone Policy.pdf"),
    "humana": Path("Humana/humana_policies/Acupuncture_for_Chronic_Lower_Back_Pain_Revised-2919704a.pdf"),
    "devoted": Path("devoted_health/policies/ABNs_and_GY_Modifiers.pdf"),
}


def test_home_summary():
    print("\n" + "=" * 75)
    print("TEST 1: Home KPIs & Baseline Summary")
    print("=" * 75)
    r = client.get("/api/summary")
    assert r.status_code == 200, f"Failed /api/summary: {r.text}"
    data = r.json()

    print(f"Total Rules:     {data['total_rules']}")
    print(f"Total CPT Codes: {data['total_cpt_codes']}")
    print(f"Total Pods:      {data['total_pods']}")
    for pod in data["pods"]:
        print(f"  - {pod['name']:<20} ({pod['id']}): Rules={pod['rules_count']:>4}, CPTs={pod['cpt_count']:>4}")

    assert data["total_rules"] == 1292, f"Expected 1292 rules, got {data['total_rules']}"
    assert data["total_cpt_codes"] == 2895, f"Expected 2895 CPT codes, got {data['total_cpt_codes']}"
    assert data["total_pods"] == 3, f"Expected 3 pods, got {data['total_pods']}"
    print("✓ TEST 1 PASSED: Baseline KPIs match exactly!")


def test_pod_run(pod_id: str):
    print("\n" + "=" * 75)
    print(f"TEST 2: Upload and Real Pipeline Execution for Pod '{pod_id}'")
    print("=" * 75)
    pdf_path = SAMPLE_PDFS[pod_id]
    assert pdf_path.exists(), f"Sample PDF not found: {pdf_path}"

    with open(pdf_path, "rb") as f:
        file_bytes = f.read()

    print(f"Uploading {pdf_path.name} ({len(file_bytes)/1024:.1f} KB)...")
    r = client.post(
        f"/api/pods/{pod_id}/runs",
        files={"file": (pdf_path.name, file_bytes, "application/pdf")},
    )
    assert r.status_code == 200, f"Upload failed: {r.text}"
    run_id = r.json()["run_id"]
    print(f"Run started with ID: {run_id}")

    # Poll status until done or failed
    max_wait = 180  # seconds
    start_t = time.time()
    last_status = None

    while time.time() - start_t < max_wait:
        r_stat = client.get(f"/api/runs/{run_id}")
        assert r_stat.status_code == 200
        run_data = r_stat.json()
        current_status = run_data["status"]

        if current_status != last_status:
            print(f"  Status changed to: {current_status}")
            last_status = current_status

        if current_status == "done":
            break
        elif current_status == "failed":
            raise RuntimeError(f"Run {run_id} failed: {run_data.get('error')}\nLogs: {run_data['steps']}")

        time.sleep(1.5)

    assert last_status == "done", f"Pipeline did not finish in {max_wait}s"
    print(f"Pipeline completed in {time.time() - start_t:.1f}s!")
    print(f"  Extraction:      {run_data['steps'][1]['details']} ({run_data['steps'][1]['elapsed']}s)")
    print(f"  Rule Generation: {run_data['steps'][2]['details']} ({run_data['steps'][2]['elapsed']}s)")

    # Verify run artifacts exist
    run_dir = Path("pipeline_demo/data/runs") / run_id
    ext_json = run_dir / "extraction.json"
    rules_wb = run_dir / f"billing_rules_{PODS[pod_id]['client_key']}.xlsx"

    assert ext_json.exists(), f"Missing extraction.json in {run_dir}"
    assert rules_wb.exists(), f"Missing {rules_wb.name} in {run_dir}"
    print(f"✓ Output files verified: {ext_json.name}, {rules_wb.name}")

    # Test /api/runs/{run_id}/rules endpoint
    r_rules = client.get(f"/api/runs/{run_id}/rules")
    assert r_rules.status_code == 200
    rules_resp = r_rules.json()
    print(f"Retrieved {len(rules_resp['rules'])} rules via API (Total reported: {rules_resp['total']})")
    assert len(rules_resp["rules"]) == rules_resp["total"]
    assert rules_resp["total"] == run_data["metrics"]["rules_generated"]
    print("✓ TEST 2 PASSED: Rules table row count equals workbook rules count!")

    # Test compare endpoint
    print("\n" + "-" * 75)
    print(f"TEST 3: Cross-Payer Comparison for Pod '{pod_id}' (Run {run_id})")
    print("-" * 75)
    r_comp = client.post(f"/api/runs/{run_id}/compare")
    assert r_comp.status_code == 200, f"Compare failed: {r_comp.text}"
    comp_data = r_comp.json()
    summary = comp_data["summary"]

    print(f"Comparison Summary vs Peer Payers ({', '.join(comp_data['other_payers'])}):")
    print(f"  - Matched Pairs:    {summary['matched']}")
    print(f"  - Differing Rules:  {summary['differing']}")
    print(f"  - Possible Conflict:{summary['conflicting']}")
    print(f"  - Unmatched Rules:  {summary['unmatched']}")
    print(f"  - Total Rule Pairs: {summary['total_pairs']}")

    assert comp_data["current_payer"] == PODS[pod_id]["payer_name"]
    assert len(comp_data["rules"]) == rules_resp["total"]
    print("✓ TEST 3 PASSED: Comparison successfully executed and filtered to current pod!")

    return run_id


def main():
    print("=" * 80)
    print("RUNNING AUTOMATED VERIFICATION SUITE FOR PIPELINE_DEMO")
    print("=" * 80)

    test_home_summary()

    # Test UHC pod
    uhc_run_id = test_pod_run("uhc")

    # Test Humana pod
    humana_run_id = test_pod_run("humana")

    # Test Devoted pod
    devoted_run_id = test_pod_run("devoted")

    # Test safe file downloads
    print("\n" + "=" * 75)
    print("TEST 4: Artifact Downloads & Security Boundary Verification")
    print("=" * 75)
    r_dl = client.get(f"/api/runs/{uhc_run_id}/files/extraction.json")
    assert r_dl.status_code == 200
    assert len(r_dl.content) > 0
    print("✓ extraction.json download verified.")

    r_wb = client.get(f"/api/runs/{uhc_run_id}/files/billing_rules_uhc.xlsx")
    assert r_wb.status_code == 200
    assert len(r_wb.content) > 0
    print("✓ billing_rules_uhc.xlsx download verified.")

    r_bad = client.get(f"/api/runs/{uhc_run_id}/files/..%2F..%2Fbackend%2Fapp.py")
    assert r_bad.status_code == 403, f"Expected 403 Forbidden, got {r_bad.status_code}"
    print("✓ Directory traversal guard verified.")

    print("\n" + "=" * 80)
    print("ALL TESTS PASSED SUCCESSFULLY! DEMO PIPELINE IS 100% OPERATIONAL.")
    print("=" * 80)


if __name__ == "__main__":
    main()
