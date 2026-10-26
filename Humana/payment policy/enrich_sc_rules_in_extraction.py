#!/usr/bin/env python3
"""
enrich_sc_rules_in_extraction.py
================================
Parses all 185 South Carolina Medicaid initial code editing rules (SC001 to SC185)
from `LC10134SC0221_South_Carolina_initial_code_editing_rules-f73f121c.pdf` and injects
them as fully-structured rule blocks into `humana_policies/extraction_rules.json` and
mirrored `extracted_rules.json`.
"""

import os
import re
import json
from pathlib import Path
import pypdf

BASE_DIR = Path(r"c:\Payer Policy\Humana\payment policy")
PDF_PATH = BASE_DIR / "humana_policies" / "pdfs" / "LC10134SC0221_South_Carolina_initial_code_editing_rules-f73f121c.pdf"
EXTRACTION_JSON = BASE_DIR / "humana_policies" / "extraction_rules.json"
MIRROR_EXTRACTION_JSON = BASE_DIR / "extracted_rules.json"
POLICY_JSON = BASE_DIR / "humana_policies" / "extracted_policies" / "LC10134SC0221_South_Carolina_initial_code_editing_rules.json"

def clean_txt(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r'[\r\n\t]+', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    t = t.replace('\ufffd', '-')
    return t.strip()

def extract_sc_rules():
    print(f"Reading PDF: {PDF_PATH}")
    reader = pypdf.PdfReader(str(PDF_PATH))
    
    page_texts = []
    for pno in range(2, len(reader.pages)):
        text = reader.pages[pno].extract_text()
        clean_t = re.sub(r'Humana\s+\d{4}\s+South Carolina Medicaid code editing\s+page\s+\d+\s+of\s+\d+', '', text)
        page_texts.append((pno + 1, clean_t))

    combined = ""
    for pno, txt in page_texts:
        combined += f"\n<<<PAGE_{pno}>>>\n" + txt

    parts = re.split(r'\nRule number\s+(SC\d+)\s*\n', combined)
    print(f"Split PDF into {len(parts)} parts.")

    extracted_blocks = []
    all_policy_codes = set()

    for i in range(1, len(parts), 2):
        r_id = parts[i]
        r_body = parts[i+1]

        # Determine page number
        pos = combined.find(f"Rule number  {r_id}")
        if pos == -1:
            pos = combined.find(f"Rule number {r_id}")
        prev_text = combined[:pos] if pos != -1 else ""
        p_match = re.findall(r'<<<PAGE_(\d+)>>>', prev_text)
        page_num = int(p_match[-1]) if p_match else 3

        body_clean = re.sub(r'<<<PAGE_\d+>>>', '', r_body)

        applies_to, category, topic, rule_stmt, rationale = "", "", "", "", ""
        m_app = re.search(r'Applies to\s+(.*?)(?=\nCategory|\Z)', body_clean, re.DOTALL)
        if m_app: applies_to = clean_txt(m_app.group(1))

        m_cat = re.search(r'Category\s+(.*?)(?=\nTopic|\Z)', body_clean, re.DOTALL)
        if m_cat: category = clean_txt(m_cat.group(1))

        m_top = re.search(r'Topic\s+(.*?)(?=\nCode editing rule|\Z)', body_clean, re.DOTALL)
        if m_top: topic = clean_txt(m_top.group(1))

        m_rule = re.search(r'Code editing rule\s+(.*?)(?=\nWhy we apply this rule|\Z)', body_clean, re.DOTALL)
        if m_rule: rule_stmt = clean_txt(m_rule.group(1))

        m_why = re.search(r'Why we apply this rule\s+(.*?)(?=\Z)', body_clean, re.DOTALL)
        if m_why: rationale = clean_txt(m_why.group(1))

        # Codes extraction
        cpts = set(re.findall(r'\b([0-9]{4}[0-9A-Z]|[A-Z][0-9]{4})\b', rule_stmt))
        ranges = re.findall(r'\b([0-9]{5})\s*[-–]\s*([0-9]{5})\b', rule_stmt)
        for start, end in ranges:
            s, e = int(start), int(end)
            if 0 < e - s <= 30:
                for c in range(s, e + 1):
                    cpts.add(str(c))

        codes_list = sorted(list(cpts))
        all_policy_codes.update(codes_list)

        block = {
            "title": f"Rule {r_id}: {topic}",
            "section": category,
            "subsection": applies_to,
            "hierarchy_path": f"South Carolina Medicaid > {category} > {topic}",
            "page_number": page_num,
            "page_start": page_num,
            "page_end": page_num,
            "content": f"[Rule {r_id}] {rule_stmt} (Applies to: {applies_to}. Rationale: {rationale})",
            "codes": codes_list,
            "table_data": None,
            "content_type": "rule_statement",
            "rule_identifier": r_id,
            "topic": topic,
            "category": category,
            "applies_to": applies_to,
            "rationale": rationale
        }
        extracted_blocks.append(block)

    print(f"Extracted {len(extracted_blocks)} rich SC rule blocks.")
    print(f"Distinct codes identified in SC rules: {len(all_policy_codes)}")

    # Update extraction_rules.json
    with open(EXTRACTION_JSON, "r", encoding="utf-8") as f:
        policies = json.load(f)

    target_idx = None
    for idx, p in enumerate(policies):
        if "LC10134SC" in p.get("source_pdf", ""):
            target_idx = idx
            break

    if target_idx is None:
        raise ValueError("Could not find LC10134SC policy in extraction_rules.json")

    print(f"Found target policy at index {target_idx}: {policies[target_idx]['policy_title']}")
    policies[target_idx]["extracted_content"] = extracted_blocks
    policies[target_idx]["mapped_cpt_codes"] = [
        {"code": c, "confidence": 0.99, "source": "Rule_Statement"} for c in sorted(list(all_policy_codes))
    ]

    with open(EXTRACTION_JSON, "w", encoding="utf-8") as f:
        json.dump(policies, f, indent=2)
    print(f"[PASS] Updated {EXTRACTION_JSON}")

    with open(MIRROR_EXTRACTION_JSON, "w", encoding="utf-8") as f:
        json.dump(policies, f, indent=2)
    print(f"[PASS] Updated {MIRROR_EXTRACTION_JSON}")

if __name__ == "__main__":
    extract_sc_rules()
