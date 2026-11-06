"""
run_comparison.py — Entry-point script for the Rule Comparison Pipeline.

Usage
-----
    cd "c:\\Users\\dell\\Documents\\Payer Policy\\Rule Comparison"
    python run_comparison.py

Outputs
-------
    comparison_report/comparison_report.xlsx
    comparison_report/executive_summary.md
"""

import sys
from pathlib import Path

# Ensure this directory is on the path so ingest/matcher/etc. can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reporter import run

if __name__ == "__main__":
    run()

