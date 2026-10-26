#!/usr/bin/env python3
"""
extract_rules.py
================
Extracts structured payer reimbursement rules and metadata from Aetna policy PDFs,
producing output conforming to the reference schema in UHC extraction_rules.json.

Output Schema per policy item:
------------------------------
- policy_title: str or None
- policy_number: str or None
- effective_date: str or None
- expiry_date: str or None
- Line_of_Business: str (e.g., "Illinois Medicaid")
- Billingclass: str ("Both", "Professional", or "Facility")
- CPT_CODES_USED: list of unique CPT / HCPCS codes
- reimbursement_guidelines: dict with { "title", "content", "subsections"?, "tables"? }
- Question_Answer_Rules: list of { "Rule N": { "Q": ..., "A": ... } } or None
- linked_pdf_contents: list or None
- resources: str or None

Usage:
    python extract_rules.py
    python extract_rules.py --pdf-dir ./aetna_il_payment_policies --out extraction_rules.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pymupdf


# --------------------------------------------------------------------------- #
# Regex Patterns
# --------------------------------------------------------------------------- #

CPT_NUM = r"\b\d{5}\b"
CPT_CAT3 = r"\b\d{4}[TU]\b"
HCPCS = r"\b[A-VX-Z]\d{4}\b"
CODE_TOKEN_REGEX = re.compile(r"\b(?:\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\b")
RANGE_REGEX = re.compile(
    r"\b(\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\s*(?:-|to|through|thru)\s*(\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\b",
    re.I,
)

# Common non-CPT 5-digit number false positives
CODE_STOP_WORDS = {"15000", "10000", "20000", "30000", "50000", "11291", "12020", "11202"}

DATE_PATTERN = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
URL_PDF_PATTERN = re.compile(r"https?://[^\s\"')>]+\.pdf", re.I)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #

def norm_ws(text: Optional[str]) -> str:
    """Normalize whitespace and strip text."""
    if not text:
        return ""
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def expand_code_range(lo: str, hi: str) -> List[str]:
    """Expand code ranges like 95076-95079 into individual codes."""
    if lo.isdigit() and hi.isdigit() and len(lo) == len(hi) == 5:
        a, b = int(lo), int(hi)
        if 0 <= b - a <= 500:
            return [f"{n:05d}" for n in range(a, b + 1)]
    return [lo, hi]


def extract_cpt_codes(text: str, tables_data: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Extract and deduplicate CPT and HCPCS codes from text and tables."""
    codes: Set[str] = set()

    # Expand ranges from text
    for m in RANGE_REGEX.finditer(text):
        for code in expand_code_range(m.group(1), m.group(2)):
            codes.add(code)

    # Individual codes from text
    for m in CODE_TOKEN_REGEX.finditer(text):
        tok = m.group(0)
        if tok not in CODE_STOP_WORDS:
            codes.add(tok)

    # Search in table cells
    if tables_data:
        for tbl in tables_data:
            content = tbl.get("content", [])
            for item in content:
                if isinstance(item, dict):
                    for val in item.values():
                        if isinstance(val, str):
                            for m in RANGE_REGEX.finditer(val):
                                for code in expand_code_range(m.group(1), m.group(2)):
                                    codes.add(code)
                            for m in CODE_TOKEN_REGEX.finditer(val):
                                tok = m.group(0)
                                if tok not in CODE_STOP_WORDS:
                                    codes.add(tok)

    return sorted(codes)


def extract_billing_class(full_text: str) -> str:
    """Determine Billingclass based on claim type mentions."""
    lower = full_text.lower()
    has_prof = "professional" in lower
    has_fac = "facility" in lower or "ub04" in lower or "ub-04" in lower or "cms 1450" in lower
    if has_prof and has_fac:
        return "Both"
    if has_fac and not has_prof:
        return "Facility"
    if has_prof and not has_fac:
        return "Professional"
    return "Both"


# --------------------------------------------------------------------------- #
# Metadata Parser
# --------------------------------------------------------------------------- #

def extract_header_metadata(doc: pymupdf.Document, fname: str) -> Dict[str, Optional[str]]:
    """Extract metadata: policy_title, policy_number, effective_date, expiry_date."""
    p0 = doc[0].get_text()
    meta: Dict[str, Optional[str]] = {
        "policy_title": None,
        "policy_number": None,
        "effective_date": None,
        "expiry_date": None,
        "Line_of_Business": "Illinois Medicaid",
        "Billingclass": "Both",
    }

    # Format 1: Table-based header on page 1 (A-ABHIL-RP-*.pdf)
    tables = list(doc[0].find_tables())
    if tables:
        for t in tables:
            rows = t.extract()
            for i, r in enumerate(rows):
                row_str = " ".join(str(c) for c in r if c)
                # Header row: Effective Date | Next Annual Review | Policy Number
                if "Effective Date" in row_str and i + 1 < len(rows):
                    next_r = rows[i + 1]
                    clean_hdr = [c for c in r if c]
                    clean_val = [c for c in next_r if c]
                    for idx, h in enumerate(clean_hdr):
                        if idx < len(clean_val):
                            h_clean = h.strip().lower()
                            v_clean = clean_val[idx].strip()
                            if "effective" in h_clean:
                                meta["effective_date"] = v_clean
                            elif "review" in h_clean or "annual" in h_clean:
                                meta["expiry_date"] = v_clean
                            elif "number" in h_clean:
                                meta["policy_number"] = v_clean

                # Header row: Policy Name | Department
                if "Policy Name" in row_str and i + 1 < len(rows):
                    clean_hdr = [c for c in r if c]
                    clean_val = [c for c in rows[i + 1] if c]
                    for idx, h in enumerate(clean_hdr):
                        if idx < len(clean_val):
                            h_clean = h.strip().lower()
                            v_clean = clean_val[idx].strip()
                            if "name" in h_clean:
                                meta["policy_title"] = v_clean

    # Format 2: Label-value pairs (112910_*.pdf and fallback)
    if not meta.get("policy_title"):
        m = re.search(r"Policy\s*Name\s*[:\n]\s*([^\n]+)", p0, re.I)
        if m:
            meta["policy_title"] = m.group(1).strip()

    if not meta.get("policy_number"):
        m = re.search(r"Policy\s*Number\s*[:\n]\s*([A-Za-z0-9_.-]+)", p0, re.I)
        if m:
            meta["policy_number"] = m.group(1).strip()

    if not meta.get("effective_date"):
        m = re.search(r"Effective\s*Date\s*[:\n]\s*([0-9/.-]+)", p0, re.I)
        if m:
            meta["effective_date"] = m.group(1).strip()

    if not meta.get("expiry_date"):
        m = re.search(r"(?:Next\s*Annual\s*Review|Review(?:ed)?)\s*[:\n]\s*([0-9/.-]+)", p0, re.I)
        if m:
            meta["expiry_date"] = m.group(1).strip()

    # Fallback title from filename
    if not meta.get("policy_title"):
        base_name = Path(fname).stem
        cleaned = re.sub(r"^(?:A-ABHIL-RP-\d+-|112910_A-IL_|112910_)", "", base_name)
        cleaned = re.sub(r"[-_](?:\d{4}|\d{2}\d{2})$", "", cleaned)
        meta["policy_title"] = cleaned.replace("_", " ").strip()

    # Fallback policy number from filename
    if not meta.get("policy_number"):
        m = re.search(r"(ABHIL-RP-\d+)", fname, re.I)
        if m:
            meta["policy_number"] = m.group(1).upper()
        else:
            meta["policy_number"] = "XXXX.XX"

    # Line of Business
    if "medicaid" in p0.lower():
        meta["Line_of_Business"] = "Illinois Medicaid"

    return meta


# --------------------------------------------------------------------------- #
# Table Extractor
# --------------------------------------------------------------------------- #

def extract_tables_for_section(doc: pymupdf.Document, start_page: int, end_page: int) -> List[Dict[str, Any]]:
    """Extract structured tables occurring between start_page and end_page."""
    extracted_tables = []
    seen_tables = set()

    for pno in range(start_page, min(end_page + 1, len(doc))):
        page = doc[pno]
        try:
            tables = list(page.find_tables())
        except Exception:
            continue

        for tbl in tables:
            rows = tbl.extract()
            if not rows or len(rows) < 1:
                continue

            # Skip header metadata tables and TOC tables
            first_row_str = " ".join(str(c) for c in rows[0] if c).lower()
            if "effective date" in first_row_str or "policy name" in first_row_str:
                continue
            if len(rows) == 1 and ("reimbursement guidelines" in first_row_str or "overview" in first_row_str):
                continue

            content_rows = []
            for r in rows:
                clean_r = [norm_ws(c) for c in r if c is not None and norm_ws(c)]
                if not clean_r:
                    continue
                if len(clean_r) == 1:
                    content_rows.append({"key": clean_r[0], "value": ""})
                elif len(clean_r) == 2:
                    content_rows.append({"key": clean_r[0], "value": clean_r[1]})
                else:
                    content_rows.append({"key": clean_r[0], "value": " | ".join(clean_r[1:])})

            if content_rows:
                tbl_repr = tuple((item["key"], item["value"]) for item in content_rows)
                if tbl_repr not in seen_tables:
                    seen_tables.add(tbl_repr)
                    extracted_tables.append({
                        "type": "table",
                        "content": content_rows,
                    })

    return extracted_tables


# --------------------------------------------------------------------------- #
# Section & Guidelines Extractor
# --------------------------------------------------------------------------- #

def extract_sections(doc: pymupdf.Document) -> Dict[str, Tuple[str, int, int]]:
    """
    Extract major lettered/named sections with their text and page spans.
    Returns: dict mapping section name -> (section_text, start_page, end_page)
    """
    page_texts = []
    for p_idx, page in enumerate(doc):
        raw_text = page.get_text()
        # Filter out repeated running headers / footers
        lines = []
        for line in raw_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("Aetna Better Health") or stripped.startswith("aetnabetterhealth.com"):
                continue
            lines.append(line)
        page_texts.append((p_idx, "\n".join(lines)))

    full_text = "\n".join(f"\n<<<PAGE_{p}>>>\n{t}" for p, t in page_texts)

    # 1. Lettered sections (A. Policy, B. Overview, C. Definitions, D. Reimbursement Guidelines, etc.)
    heading_pattern = re.compile(r"\n\s*([A-H]\.\s+[^\n]+)", re.I)
    matches = list(heading_pattern.finditer(full_text))

    sections: Dict[str, Tuple[str, int, int]] = {}

    for i, m in enumerate(matches):
        heading = norm_ws(m.group(1))
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        content_chunk = full_text[start_pos:end_pos].strip()

        # Skip Table of Contents matches (which consist solely of page numbers or are very brief)
        if len(content_chunk) <= 10 or re.fullmatch(r"\d+", content_chunk.strip()):
            continue

        # Extract page range
        pre_chunk = full_text[:start_pos]
        page_tags_pre = re.findall(r"<<<PAGE_(\d+)>>>", pre_chunk)
        start_p = int(page_tags_pre[-1]) if page_tags_pre else 0

        during_tags = re.findall(r"<<<PAGE_(\d+)>>>", full_text[start_pos:end_pos])
        end_p = int(during_tags[-1]) if during_tags else start_p

        # Clean page markers from content
        cleaned_content = re.sub(r"<<<PAGE_\d+>>>\n?", "", content_chunk).strip()

        h_key = re.sub(r"^[A-H]\.\s*", "", heading).strip()
        sections[h_key] = (cleaned_content, start_p, end_p)

    # 2. Older format sections (PURPOSE:, STATEMENT OF OBJECTIVE:, REIMBURSEMENT GUIDELINES:, etc.)
    if not sections:
        old_pattern = re.compile(r"\n\s*([A-Z/ -]{4,}):\s*", re.M)
        old_matches = list(old_pattern.finditer(full_text))
        for i, m in enumerate(old_matches):
            heading = norm_ws(m.group(1))
            start_pos = m.end()
            end_pos = old_matches[i + 1].start() if i + 1 < len(old_matches) else len(full_text)
            content_chunk = full_text[start_pos:end_pos].strip()

            pre_chunk = full_text[:start_pos]
            page_tags_pre = re.findall(r"<<<PAGE_(\d+)>>>", pre_chunk)
            start_p = int(page_tags_pre[-1]) if page_tags_pre else 0

            during_tags = re.findall(r"<<<PAGE_(\d+)>>>", full_text[start_pos:end_pos])
            end_p = int(during_tags[-1]) if during_tags else start_p

            cleaned_content = re.sub(r"<<<PAGE_\d+>>>\n?", "", content_chunk).strip()
            sections[heading] = (cleaned_content, start_p, end_p)

    return sections


def build_subsections(content: str) -> List[Dict[str, str]]:
    """Parse distinct subsections / criteria blocks from reimbursement guidelines prose."""
    subsections: List[Dict[str, str]] = []

    # Case 1: Bulleted criteria blocks
    bullet_blocks = re.split(r"\n\s*[•\u2022\u25cf\-]\s*", content)
    if len(bullet_blocks) > 1:
        for b in bullet_blocks[1:]:
            lines = [norm_ws(l) for l in b.split("\n") if norm_ws(l)]
            if lines:
                title = lines[0]
                body = " ".join(lines[1:]) if len(lines) > 1 else title
                subsections.append({
                    "title": title,
                    "content": body,
                })
        return subsections

    # Case 2: Paragraph / subheader blocks
    paras = [p.strip() for p in content.split("\n\n") if p.strip()]
    for p in paras:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        if len(lines) >= 2 and len(lines[0]) <= 80:
            subsections.append({
                "title": lines[0],
                "content": " ".join(lines[1:]),
            })

    return subsections


def extract_reimbursement_guidelines(
    doc: pymupdf.Document,
    sections: Dict[str, Tuple[str, int, int]],
) -> Dict[str, Any]:
    """Build the reimbursement_guidelines payload matching the reference schema."""
    rg_data: Dict[str, Any] = {
        "title": "Reimbursement Guidelines",
        "content": "",
    }

    rg_tuple = None
    # Check Section D or REIMBURSEMENT GUIDELINES
    for k in ("Reimbursement Guidelines", "REIMBURSEMENT GUIDELINES", "Reimbursement Guideline"):
        if k in sections:
            rg_tuple = sections[k]
            break

    # Older 1-page fallback: PURPOSE + STATEMENT OF OBJECTIVE
    if not rg_tuple:
        purpose_tuple = sections.get("PURPOSE")
        obj_tuple = sections.get("STATEMENT OF OBJECTIVE")
        if purpose_tuple or obj_tuple:
            text_parts = []
            start_p, end_p = 0, 0
            if purpose_tuple:
                text_parts.append(purpose_tuple[0])
                start_p = purpose_tuple[1]
                end_p = purpose_tuple[2]
            if obj_tuple:
                text_parts.append(obj_tuple[0])
                end_p = max(end_p, obj_tuple[2])
            rg_tuple = ("\n\n".join(text_parts), start_p, end_p)

    if rg_tuple:
        content_text, start_p, end_p = rg_tuple
        rg_data["content"] = norm_ws(content_text)

        # Extract subsections
        subsections = build_subsections(content_text)
        if subsections:
            rg_data["subsections"] = subsections

        # Extract tables in the guideline page range
        tables = extract_tables_for_section(doc, start_p, end_p)
        if tables:
            rg_data["tables"] = tables

    return rg_data


def extract_qa_rules(sections: Dict[str, Tuple[str, int, int]]) -> Optional[List[Dict[str, Any]]]:
    """Extract Q&A rules in the Question_Answer_Rules format if present."""
    qa_tuple = None
    for k in ("Frequently Asked Questions", "Questions and Answers", "Question and Answers", "QUESTIONS AND ANSWERS"):
        if k in sections:
            qa_tuple = sections[k]
            break

    if not qa_tuple:
        return None

    qa_text = qa_tuple[0]
    if not qa_text or qa_text.strip() == "N/A":
        return None

    # Parse Q: ... A: ... blocks
    qa_pattern = re.compile(r"Q(?:uestion)?\s*[:\d.]*\s*([^\n?]+(?:\?|[^\n]+))\s*\n\s*A(?:nswer)?\s*[:\d.]*\s*([^\n]+(?:\n[^\n]+)*?)(?=(?:\s*Q|\Z))", re.I)
    rules = []
    rule_idx = 1
    for m in qa_pattern.finditer(qa_text):
        q_text = norm_ws(m.group(1))
        a_text = norm_ws(m.group(2))
        rules.append({
            f"Rule {rule_idx}": {
                "Q": q_text,
                "A": a_text,
            }
        })
        rule_idx += 1

    return rules if rules else None


def extract_resources(sections: Dict[str, Tuple[str, int, int]]) -> Optional[str]:
    """Extract content of Resources / Reference section."""
    for k in ("Resources", "RESOURCES", "LEGAL/CONTRACT REFERENCE", "References", "REFERENCES"):
        if k in sections:
            text = sections[k][0].strip()
            if text and text != "N/A":
                return norm_ws(text)
    return None


def extract_linked_pdfs(full_text: str) -> Optional[List[Dict[str, Any]]]:
    """Find any linked PDF URLs referenced in the policy."""
    urls = sorted(set(URL_PDF_PATTERN.findall(full_text)))
    if not urls:
        return None
    return [{"url": u, "content": []} for u in urls]


# --------------------------------------------------------------------------- #
# Main Policy Processor
# --------------------------------------------------------------------------- #

def process_policy_pdf(pdf_path: Path) -> Dict[str, Any]:
    """Process a single policy PDF and return a structured rule dictionary."""
    doc = pymupdf.open(str(pdf_path))
    fname = pdf_path.name

    # Full concatenated text
    full_text = "\n".join(page.get_text() for page in doc)

    # 1. Header metadata
    meta = extract_header_metadata(doc, fname)
    meta["Billingclass"] = extract_billing_class(full_text)

    # 2. Sections
    sections = extract_sections(doc)

    # 3. Reimbursement guidelines
    rg_data = extract_reimbursement_guidelines(doc, sections)

    # 4. Codes section tables & all CPT codes
    codes_tables = []
    for k in ("Codes/Condition of Coverage", "Codes/Conditions of Coverage", "Codes", "CODES"):
        if k in sections:
            start_p, end_p = sections[k][1], sections[k][2]
            codes_tables.extend(extract_tables_for_section(doc, start_p, end_p))

    all_tables = (rg_data.get("tables") or []) + codes_tables
    cpt_codes_used = extract_cpt_codes(full_text, all_tables)

    # 5. Q&A Rules
    qa_rules = extract_qa_rules(sections)

    # 6. Linked PDF contents
    linked_pdfs = extract_linked_pdfs(full_text)

    # 7. Resources
    resources_content = extract_resources(sections)

    # Construct the final rule matching UHC schema
    rule = {
        "policy_title": meta.get("policy_title"),
        "policy_number": meta.get("policy_number"),
        "effective_date": meta.get("effective_date"),
        "expiry_date": meta.get("expiry_date"),
        "Line_of_Business": meta.get("Line_of_Business", "Illinois Medicaid"),
        "Billingclass": meta.get("Billingclass", "Both"),
        "CPT_CODES_USED": cpt_codes_used,
        "reimbursement_guidelines": rg_data,
        "Question_Answer_Rules": qa_rules,
        "linked_pdf_contents": linked_pdfs,
        "resources": resources_content,
    }

    doc.close()
    return rule


# --------------------------------------------------------------------------- #
# Main Entry Point
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Extract structured reimbursement rules from policy PDFs in UHC format."
    )
    base_dir = Path(__file__).resolve().parent
    default_input = base_dir / "aetna_il_payment_policies"
    default_out = base_dir / "extraction_rules.json"
    default_out_dir = base_dir / "extracted_contents"

    parser.add_argument(
        "--pdf-dir",
        default=str(default_input),
        help="Directory containing policy PDFs (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        default=str(default_out),
        help="Path for compiled extraction_rules.json (default: %(default)s)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(default_out_dir),
        help="Directory to save per-policy individual JSON files (default: %(default)s)",
    )

    args = parser.parse_args()

    input_dir = Path(args.pdf_dir).resolve()
    out_file = Path(args.out).resolve()
    out_dir = Path(args.out_dir).resolve()

    if not input_dir.exists():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        sys.exit(1)

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDF files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(pdf_files)} PDF file(s) in {input_dir}")
    print(f"Extracting rules into {out_file}...")

    extracted_rules: List[Dict[str, Any]] = []

    for i, pdf_path in enumerate(pdf_files, 1):
        try:
            rule = process_policy_pdf(pdf_path)
            extracted_rules.append(rule)

            # Write individual policy JSON
            safe_fname = f"{i:03d}_{rule.get('policy_number') or 'policy'}.json"
            per_file_path = out_dir / safe_fname
            with open(per_file_path, "w", encoding="utf-8") as f:
                json.dump(rule, f, indent=2, ensure_ascii=False)

            title = rule.get("policy_title") or pdf_path.name
            pnum = rule.get("policy_number") or "N/A"
            codes_count = len(rule.get("CPT_CODES_USED", []))
            print(f"[{i:>2}/{len(pdf_files)}] {title[:40]:<40} ({pnum}) -> {codes_count} CPT/HCPCS code(s)")
        except Exception as e:
            print(f"[{i:>2}/{len(pdf_files)}] ERROR processing {pdf_path.name}: {e}", file=sys.stderr)

    # Write master extraction_rules.json
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(extracted_rules, f, indent=2, ensure_ascii=False)

    print(f"\nSuccessfully extracted rules for {len(extracted_rules)} policies.")
    print(f"Master file saved to: {out_file}")
    print(f"Individual policy JSONs saved to: {out_dir}")


if __name__ == "__main__":
    main()
