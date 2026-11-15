# Payer Policy Intelligence — UMLS-Powered CPT Knowledge Base (Python + DuckDB)

Deterministic pipeline (no LLMs, no generative AI) that:

1. Builds a local, release-pinned **terminology knowledge base** in DuckDB from the UMLS Metathesaurus (CPT, HCPCS, and synonyms from other vocabularies on the same concepts);
2. Finds **explicit** CPT/HCPCS codes in payer policies and **infers candidate codes** from policy **content** and, independently, from policy **titles**, with full evidence for every candidate;
3. Identifies **related policies across payers** with an explainable score and scope check;
4. Compares their **rules** and reports differences and possibly missing rules — flagging "not assessable" when an extraction looks incomplete.

Design rationale, repository evaluation, licensing analysis, and schema are in **[docs/DESIGN.md](docs/DESIGN.md)**.
Operational walkthrough and execution runbook: **[docs/END_TO_END_GUIDE.md](docs/END_TO_END_GUIDE.md)** and **[PIPELINE_RUNBOOK.md](PIPELINE_RUNBOOK.md)**.

---

## Multi-Payer Architecture

Each payer's extraction JSON is ingested through a **payer profile** (`config/payers/*.json`) and an adapter (`guidelines_document`, `sectioned_list`, `declarative`) into a unified canonical model; the mapping, UMLS enrichment, and cross-payer matching engine are fully shared. Profiles exist for **UnitedHealthcare**, **Humana**, and **Aetna**.

```powershell
# 1. Initialize Database
python scripts\initialize_database.py

# 2. Build ICD-10-CM Diagnosis Index
python scripts\build_icd10cm_index.py

# 3. Ingest and Map Policies (Run per payer)
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_uhc.json --payer-profile unitedhealthcare
python scripts\map_policy_cpt_codes.py --input data\input\extracted_rules_humana.json --payer-profile humana
python scripts\map_policy_cpt_codes.py --input data\input\extraction_rules_aetna.json --payer-profile aetna

# 4. Cross-Payer Policy & Rule Gap Analysis
python scripts\compare_policies.py

# 5. Integrity Validation & Regression Tests
python scripts\validate_results.py
python -m pytest tests
```

Audit reports are generated in `data\exports\audit\` (extraction health, unresolved mappings, provenance, code-list policies, quality signals, and section statuses).
