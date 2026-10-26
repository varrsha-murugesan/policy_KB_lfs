# KB Creation v1: Pipeline Runbook & End-to-End Guide

A comprehensive, self-contained operational and architectural runbook has been generated for this repository.

👉 **View the complete documentation here:** [docs/END_TO_END_GUIDE.md](file:///c:/Payer%20Policy/KB%20creation%20v1/docs/END_TO_END_GUIDE.md)

---

### Quick Execution Cheat Sheet

From the root directory in PowerShell:

```powershell
# 1. Initialize Database Schema
python scripts\initialize_database.py

# 2. Ingest & Map Policies (Run per payer)
python scripts\map_policy_cpt_codes.py --input data\input\humana_extracted_contents.json --payer-profile humana
python scripts\map_policy_cpt_codes.py --input data\input\unitedhealthcare_extracted_contents.json --payer-profile unitedhealthcare
python scripts\map_policy_cpt_codes.py --input data\input\aetna_extracted_contents.json --payer-profile aetna

# 3. Compare Policies & Identify Rule Gaps Across Payers
python scripts\compare_policies.py

# 4. Run Structural & Integrity Checks
python scripts\validate_results.py
```

### Primary Output Locations
- **Master CPT/HCPCS Mappings:** `data/exports/policy_cpt_mappings.csv`
- **Mapping Evidence Trail:** `data/exports/mapping_evidence.json`
- **Extracted Code Tokens:** `data/exports/code_mentions.csv`
- **Policy Comparison Matrix:** `data/exports/policy_comparison_report.csv`
- **Rule Discrepancy & Gap Log:** `data/exports/rule_gap_report.csv`
- **Extraction Audit & Diagnostics:** `data/exports/audit/`
