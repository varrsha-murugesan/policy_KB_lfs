#!/usr/bin/env python3
"""
update_extraction_rules_with_cpt.py
===================================
Enriches extraction_rules.json with the clinically validated CPT and HCPCS
codes mapped from the Knowledge Base (KB).

Updates:
1. `humana_policies/extraction_rules.json` (master input for rule generator)
2. `extracted_rules.json` (mirrored copy in payment policy root)
3. Corresponding policy JSONs in `humana_policies/extracted_policies/*.json`
"""

import os
import json
import duckdb
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(r"c:\Payer Policy\Humana\payment policy")
KB_DB_PATH = Path(r"c:\Payer Policy\KB creation v1\database\payer_policy_kb.duckdb")
EXTRACTION_RULES_FILE = BASE_DIR / "humana_policies" / "extraction_rules.json"
MIRROR_RULES_FILE = BASE_DIR / "extracted_rules.json"
EXTRACTED_POLICIES_DIR = BASE_DIR / "humana_policies" / "extracted_policies"

# Curated noise filter: phrases like 'autopsy' that matched generic words in clinical policies
EXCLUDED_NOISE_CODES = {"88036", "88037"}

def load_kb_mappings():
    con = duckdb.connect(str(KB_DB_PATH), read_only=True)
    run_id = "map-20261026T155017-ac17d6"

    query = """
        SELECT 
            p.policy_title,
            p.source_pdf,
            m.code_system,
            m.code,
            c.description,
            m.confidence_tier,
            m.score,
            m.best_method,
            m.mapping_origin
        FROM policy_cpt_mappings m
        JOIN policies p ON p.policy_uid = m.policy_uid
        LEFT JOIN cpt_codes c ON c.code_system = m.code_system AND c.code = m.code
        WHERE m.run_id = ?
        ORDER BY p.policy_title, m.confidence_tier, m.score DESC, m.code
    """
    rows = con.execute(query, [run_id]).fetchall()
    con.close()

    mappings_by_title = defaultdict(list)
    mappings_by_pdf = defaultdict(list)

    for r in rows:
        title, src_pdf, sys, code, desc, tier, score, method, origin = r
        if code in EXCLUDED_NOISE_CODES and "Autopsy" not in title:
            continue
        
        entry = {
            "code": code,
            "code_system": sys,
            "description": desc or "",
            "confidence_tier": tier,
            "score": score,
            "mapping_origin": origin,
            "best_method": method
        }
        mappings_by_title[title].append(entry)
        if src_pdf:
            mappings_by_pdf[src_pdf].append(entry)

    return mappings_by_title, mappings_by_pdf

def update_extraction_rules():
    print("=" * 80)
    print("UPDATING EXTRACTION_RULES.JSON WITH MAPPED CPT/HCPCS CODES")
    print("=" * 80)

    mappings_by_title, mappings_by_pdf = load_kb_mappings()
    print(f"Loaded mappings for {len(mappings_by_title)} candidate policies from KB.")

    if not EXTRACTION_RULES_FILE.exists():
        raise FileNotFoundError(f"Missing master extraction rules: {EXTRACTION_RULES_FILE}")

    with open(EXTRACTION_RULES_FILE, "r", encoding="utf-8") as f:
        policies = json.load(f)

    print(f"Total policies in extraction_rules.json: {len(policies)}")

    updated_policy_count = 0
    updated_block_count = 0
    total_codes_injected = 0

    for policy in policies:
        title = policy.get("policy_title", "")
        src_pdf = policy.get("source_pdf", "")

        # Find matching mappings
        mapped_list = mappings_by_title.get(title) or mappings_by_pdf.get(src_pdf)
        if not mapped_list:
            continue

        # Deduplicate codes while preserving highest score / tier
        seen_codes = set()
        deduped_mappings = []
        for m in mapped_list:
            if m["code"] not in seen_codes:
                seen_codes.add(m["code"])
                deduped_mappings.append(m)

        if not deduped_mappings:
            continue

        # Add mapped_cpt_codes to policy root
        policy["mapped_cpt_codes"] = deduped_mappings
        updated_policy_count += 1
        total_codes_injected += len(deduped_mappings)

        # Extract code strings for block injection
        code_strings = [m["code"] for m in deduped_mappings]

        # Inject into extracted_content blocks where appropriate
        extracted_content = policy.get("extracted_content", [])
        for block in extracted_content:
            existing_codes = block.get("codes", [])
            # If block has no codes or only modifier tags, attach primary mapped CPT codes
            if not any(len(c) == 5 and c.isdigit() for c in existing_codes):
                # Only attach to non-boilerplate, rule-bearing sections
                sec_title = (block.get("title") or "").lower()
                sec_name = (block.get("section") or "").lower()
                if not any(ign in sec_title or ign in sec_name for ign in ["contact", "publication", "reference", "disclaimer"]):
                    combined_codes = sorted(list(set(existing_codes + code_strings[:8])))
                    block["codes"] = combined_codes
                    updated_block_count += 1

        # Also update corresponding individual JSON in extracted_policies/
        stem = Path(src_pdf).stem
        # find matching json in extracted_policies
        matched_indiv = None
        for candidate_name in [f"{title.replace(' ', '_')}.json", f"{stem}.json"]:
            candidate_path = EXTRACTED_POLICIES_DIR / candidate_name
            if candidate_path.exists():
                matched_indiv = candidate_path
                break
        
        if not matched_indiv:
            # search directory by title match
            for jf in EXTRACTED_POLICIES_DIR.glob("*.json"):
                if title.lower() in jf.stem.lower() or jf.stem.lower() in title.lower():
                    matched_indiv = jf
                    break

        if matched_indiv and matched_indiv.exists():
            try:
                with open(matched_indiv, "r", encoding="utf-8") as f_indiv:
                    indiv_data = json.load(f_indiv)
                
                clin = indiv_data.setdefault("clinical_codes", {})
                current_cpts = clin.get("cpt_codes", [])
                new_cpts = [m["code"] for m in deduped_mappings if len(m["code"]) == 5 and m["code"].isdigit()]
                merged_cpts = sorted(list(set(current_cpts + new_cpts)))
                clin["cpt_codes"] = merged_cpts

                all_codes = clin.get("all_codes", [])
                all_new = [m["code"] for m in deduped_mappings]
                clin["all_codes"] = sorted(list(set(all_codes + all_new)))

                indiv_data["mapped_cpt_codes"] = deduped_mappings

                with open(matched_indiv, "w", encoding="utf-8") as f_indiv:
                    json.dump(indiv_data, f_indiv, indent=2)
            except Exception as e:
                print(f"Warning: Could not update individual JSON {matched_indiv.name}: {e}")

    # Write updated master extraction_rules.json
    with open(EXTRACTION_RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(policies, f, indent=2)
    print(f"[PASS] Successfully updated {EXTRACTION_RULES_FILE}")

    # Write updated mirror extracted_rules.json
    with open(MIRROR_RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(policies, f, indent=2)
    print(f"[PASS] Successfully updated mirror copy {MIRROR_RULES_FILE}")

    print("\n" + "=" * 80)
    print("EXTRACTION_RULES.JSON UPDATE METRICS")
    print("=" * 80)
    print(f"Total Policies in Corpus:                {len(policies)}")
    print(f"Candidate Policies Enriched with CPTs:   {updated_policy_count}")
    print(f"Total Unique CPT/HCPCS Codes Attached:   {total_codes_injected}")
    print(f"Candidate Rule Content Blocks Enriched:  {updated_block_count}")
    print("=" * 80)

if __name__ == "__main__":
    update_extraction_rules()
