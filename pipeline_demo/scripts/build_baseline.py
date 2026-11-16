#!/usr/bin/env python3
"""
build_baseline.py
=================
Generates the baseline billing rules workbooks for UHC, Humana, and Devoted Health.
Reads the original master extraction JSON files (read-only) and invokes each pod's copied generator,
outputting to pipeline_demo/data/baseline/billing_rules_<client>.xlsx.
"""

import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEMO_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = DEMO_DIR.parent
BASELINE_DIR = DEMO_DIR / "data" / "baseline"

BASELINE_DIR.mkdir(parents=True, exist_ok=True)

PODS_CONFIG = [
    {
        "id": "uhc",
        "name": "United Healthcare",
        "script": DEMO_DIR / "pods" / "uhc" / "generate_billing_rules_uhc.py",
        "input_json": PROJECT_ROOT / "Uhc" / "extraction_rules_uhc.json",
        "output_xlsx": BASELINE_DIR / "billing_rules_uhc.xlsx",
        "cwd": DEMO_DIR / "pods" / "uhc",
        "extra_args": ["--lob", "all"],
    },
    {
        "id": "humana",
        "name": "Humana",
        "script": DEMO_DIR / "pods" / "humana" / "generate_billing_rules_humana.py",
        "input_json": PROJECT_ROOT / "Humana" / "extraction_rules_humana.json",
        "output_xlsx": BASELINE_DIR / "billing_rules_humana.xlsx",
        "cwd": DEMO_DIR / "pods" / "humana",
        "extra_args": [],
    },
    {
        "id": "devoted",
        "name": "Devoted Health",
        "script": DEMO_DIR / "pods" / "devoted" / "generate_billing_rules_devoted.py",
        "input_json": PROJECT_ROOT / "devoted_health" / "extraction_rules_devoted.json",
        "output_xlsx": BASELINE_DIR / "billing_rules_devoted.xlsx",
        "cwd": DEMO_DIR / "pods" / "devoted",
        "extra_args": [],
    },
]


def build_all():
    print("=" * 80)
    print("BUILDING BASELINE BILLING RULES WORKBOOKS")
    print("=" * 80)

    for pod in PODS_CONFIG:
        print(f"\n---> Building baseline for {pod['name']} ({pod['id']})...")
        if not pod["input_json"].exists():
            print(f"ERROR: Input JSON not found: {pod['input_json']}")
            sys.exit(1)

        cmd = [
            sys.executable,
            str(pod["script"]),
            "--input", str(pod["input_json"]),
            "--out-dir", str(BASELINE_DIR),
            "--compare-dir", "",
        ] + pod["extra_args"]

        print(f"Running: {' '.join(cmd)}")
        res = subprocess.run(cmd, cwd=str(pod["cwd"]), capture_output=True, text=True, encoding="utf-8")

        if res.returncode != 0:
            print(f"ERROR generating baseline for {pod['name']}:")
            print(res.stderr or res.stdout)
            sys.exit(res.returncode)

        if not pod["output_xlsx"].exists():
            print(f"ERROR: Expected workbook not created: {pod['output_xlsx']}")
            sys.exit(1)

        print(f"SUCCESS: Created {pod['output_xlsx'].name} ({pod['output_xlsx'].stat().st_size / 1024:.1f} KB)")

    print("\n" + "=" * 80)
    print("ALL BASELINE WORKBOOKS GENERATED SUCCESSFULLY")
    print(f"Location: {BASELINE_DIR}")
    print("=" * 80)


if __name__ == "__main__":
    build_all()
