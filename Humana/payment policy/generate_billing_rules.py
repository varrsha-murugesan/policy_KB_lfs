#!/usr/bin/env python3
"""
generate_billing_rules.py
=========================
Humana Deterministic Billing Rule Generation Engine
Consumes enriched `humana_policies/extraction_rules.json` and emits clear-cut,
easily interpretable, query-ready rules for concept development engineers into:
  - `humana_policies/billing_rules.xlsx`
  - `humana_policies/billing_rules.json`
  - `billing_rules.json` (mirrored in payment policy root)
  - `billing_rules.xlsx` (mirrored in payment policy root)

Sheets Generated in Excel:
  1. Rules        - Master structured rules table with conditions, modifiers, and claim columns
  2. Code_Index   - Mapping of each distinct CPT/HCPCS code to rules and audit actions
  3. Query_Logic  - Dedicated sheet for concept developers with pseudo-SQL query specifications
  4. Summary      - Executive metrics and breakdown by rule category, LOB, and billing class
"""

import os
import sys
import re
import json
import unicodedata
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict, Counter
import pandas as pd

BASE_DIR = Path(r"c:\Payer Policy\Humana\payment policy")
INPUT_JSON = BASE_DIR / "humana_policies" / "extraction_rules.json"
OUTPUT_XLSX = BASE_DIR / "humana_policies" / "billing_rules.xlsx"
OUTPUT_JSON = BASE_DIR / "humana_policies" / "billing_rules.json"
MIRROR_XLSX = BASE_DIR / "billing_rules.xlsx"
MIRROR_JSON = BASE_DIR / "billing_rules.json"

# ==============================================================================
# PATTERNS FOR RULE CLASSIFICATION & CONDITION EXTRACTION
# ==============================================================================
DENIAL_RE = re.compile(
    r"\b(do not reimburse|will not reimburse|does not reimburse|not reimbursable|non-?reimbursable|"
    r"denied|not covered|non-?covered|disallowed|inappropriate|prohibit(?:ed)?|not eligible for reimbursement)\b", re.I
)
REIMBURSEMENT_RE = re.compile(
    r"\b(reimburse[sd]?|reimbursement|payable|paid at|allow(?:ed|able)?|discount(?:ed)?|reduction|fee schedule|"
    r"covered only|only when|subject to other plan requirements)\b", re.I
)
FREQUENCY_RE = re.compile(
    r"\b(once per|per day|per calendar|per month|per year|maximum units?|exceeds? \d+|time limit|"
    r"no more than|lifetime limit|daily limit|limited to \d+|frequency|medically unlikely edit|mue|session[s]? limit)\b", re.I
)
BUNDLING_RE = re.compile(
    r"\b(bundled|inclusive|component of|mutually exclusive|incidental|unbundl(?:ed|ing)|"
    r"included in|separately reimbursable|not separately reimbursable)\b", re.I
)
MOD_PROHIBIT_RE = re.compile(
    r"\b(informational only|do not allow additional (?:base|time )?units|is not allowed|"
    r"not recognized|inappropriate to report charges|prohibit(?:ed)? modifier)\b", re.I
)
MODIFIER_RE = re.compile(
    r"\b(modifier|modifiers|append(?:ed)?|reported with modifier|must be billed with modifier)\b", re.I
)
BILL_TYPE_RE = re.compile(
    r"\b(bill types?|0[1-9][0-9][0-9A-Z]|type of bill|ub-04|revenue code[s]?|rev code[s]?)\b", re.I
)
AGE_RE = re.compile(
    r"\b(patient\s+age|age\s+(?:is\s+)?(?:under|over|less\s+than|greater\s+than|<=?|>=?|\d+\s*(?:years|months|days))|"
    r"under\s+\d+\s+years\s+of\s+age|over\s+\d+\s+years\s+of\s+age|pediatric|neonatal|geriatric|"
    r"\d+\s+years\s+of\s+age\s+or\s+(?:older|younger))\b", re.I
)
AUTH_RE = re.compile(
    r"\b(prior authorization|preauthorization|precertification|pre-service authorization)\b", re.I
)

# Known standard CMS/AMA modifiers
KNOWN_MODIFIERS_LIST = [
    "22", "24", "25", "26", "50", "51", "52", "53", "54", "55", "56", "58", "59",
    "62", "76", "77", "78", "79", "80", "81", "82", "90", "91", "93", "95", "99",
    "AA", "AD", "AS", "AT", "E1", "E2", "E3", "E4", "EY", "FA", "F1", "F2", "F3", "F4", "F5",
    "FY", "FX", "GN", "GO", "GP", "GT", "JW", "JZ", "KX", "LT", "P1", "P2", "P3", "P4", "P5", "P6",
    "Q0", "QK", "QX", "QY", "QZ", "RA", "RB", "RE", "RR", "RT", "TC", "XE", "XP", "XS", "XU"
]

POLICY_SLUG_MAP = {
    "Acupuncture for Chronic Lower Back Pain": "ACU",
    "Ambulance Services": "AMB",
    "Anesthesia Services": "ANES",
    "Assistant at Surgery Reimbursement": "ASST",
    "Automobile and Workplace Injuries": "AUTO",
    "Cell and Gene Therapy (CGT) Access Model": "CGT",
    "Chiropractic Services for Acute Treatment": "CHIRO",
    "Chronic Care Management and Principal Care Management": "CCM",
    "Common CPT and HCPCS Modifiers": "MOD",
    "DMEPOS Proof of Delivery Documentation": "DME",
    "DME for Medicare Advantage": "DMEMA",
    "Emergency Department E/M Reimbursement": "EDEM",
    "Gender-Specific Services for Transgender or Intersex Members": "GEND",
    "Initial Preventive Physical Examination and Annual Wellness Visit": "AWV",
    "Inpatient to Outpatient Rebilling": "REBILL",
    "LC10134SC0221 South Carolina initial code editing rules": "SC",
    "Laboratory - Allergen Testing": "ALLERG",
    "Laboratory - Pathogen Panel Testing": "PATHPNL",
    "Laboratory - Biomarkers for Myocardial Infarction and Chronic Heart Failure": "BIOMRK",
    "Multiple Procedure Payment Reduction": "MPPR",
    "Non-Covered Experimental and Investigational Services": "NONCOV",
    "Observation Services": "OBS",
    "Preventive Services": "PREV",
    "Prolonged Services": "PROLONG",
    "Split/Shared Evaluation and Management Services": "SPLIT",
    "Telehealth and Other Virtual Services": "TELE",
    "Urgent Care Center Services": "URGENT"
}

ANES_MOD_DESC = {
    "AA": "Anesthesia services performed personally by anesthesiologist",
    "AD": "Medical supervision by a physician: more than 4 concurrent anesthesia procedures",
    "QK": "Medical direction of 2, 3, or 4 concurrent anesthesia procedures involving qualified individuals",
    "QX": "CRNA service with medical direction by a physician",
    "QY": "Medical direction of one CRNA by an anesthesiologist",
    "QZ": "CRNA service without medical direction by a physician"
}

def clean_text(text: str) -> str:
    if not text:
        return ""
    # Normalize mathematical stylized letters and accents
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[\r\n\t]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    # Fix encoding artifacts
    text = text.replace('\ufffd', '-').replace('\u2019', "'").replace('\u2018', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '-')
    text = text.replace('\u00ad', '').replace('\u2011', '-')
    text = text.replace('\u25aa', '•').replace('\u273d', '•').replace('\u2192', '->')
    text = text.replace('\u00a0', ' ')
    return text.strip()

def synthesize_table_statement(text: str, policy_title: str) -> str:
    """Transforms raw table fragments into fluent, natural language billing rules."""
    if not (text.startswith("[") and "|" in text):
        return text

    # Strip web click boilerplates
    cleaned = re.sub(r'Click here[^\.]*\.?', '', text, flags=re.I).strip()

    # Extract header and key-values
    parts = cleaned.split(" | ")
    header = ""
    m_head = re.match(r'\[([^\]]+)\]\s*(.*)', parts[0])
    if m_head:
        header = m_head.group(1).strip()
        parts[0] = m_head.group(2).strip()

    kv = {}
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            kv[k.strip().lower()] = v.strip()

    # Case 1: CPT / HCPCS Modifiers Table
    if "cpt modifier" in kv or "hcpcs modifier" in kv or "modifier" in kv:
        mod = kv.get("cpt modifier") or kv.get("hcpcs modifier") or kv.get("modifier")
        desc = kv.get("short description") or kv.get("description") or ""
        reimb = kv.get("medicare advantage reimbursement") or kv.get("reimbursement") or ""
        ref = kv.get("quick reference") or ""

        # Anesthesia specific modifiers
        if header == "Common Modifiers for Anesthesia Claims" and mod in ANES_MOD_DESC:
            desc = ANES_MOD_DESC[mod]
            return f"Anesthesia Services - Modifier {mod} ({desc}): Humana Medicare Advantage reimbursement is {reimb} of the contracted fee schedule amount."

        stmt_parts = [f"Modifier {mod}"]
        if desc:
            stmt_parts.append(f"({desc})")
        stmt_parts.append(":")
        if ref:
            stmt_parts.append(ref)
        if reimb:
            stmt_parts.append(f"Medicare Advantage reimbursement: {reimb}.")
        return " ".join(stmt_parts).replace("  ", " ").strip()

    # Case 2: Assistant Surgery Calculation Table
    if "assistant" in policy_title.lower() or "contracted providers" in header.lower() or "noncontracted providers" in header.lower():
        if "contracted" in header.lower() and "amount allowed" in kv:
            return f"Assistant at Surgery Reimbursement ({header}): For eligible surgical procedures, assistant surgeon charges (modifiers 80, 81, 82, AS) are reimbursed at 16% of the primary provider's contracted surgical rate."
        elif "noncontracted" in header.lower() and "amount allowed" in kv:
            return f"Assistant at Surgery Reimbursement ({header}): For noncontracted providers, assistant surgeon charges are reimbursed at 16% of 85% of the Medicare Physician Fee Schedule (MPFS) amount."

    # Case 3: Performance Ratings / Quality Measures Table
    if "hedis" in cleaned.lower() or "cahps" in cleaned.lower() or "performance ratings" in header.lower():
        measure = kv.get("measure name") or kv.get("hedis/cahps measures required for hp accreditation-medicaid") or ""
        return f"Quality & Clinical Performance Standard ({header}): Providers must report standardized clinical quality codes for {measure} in accordance with NCQA HEDIS and state Medicaid quality requirements."

    # Case 4: Applicable procedures table (e.g. Lab and Specialty policies)
    if "applicable procedures" in header.lower() or "procedure code" in kv:
        pcode = kv.get("procedure code") or kv.get("cpt code") or kv.get("hcpcs code") or ""
        pdesc = kv.get("code description") or kv.get("description") or ""
        return f"Humana Medicare Advantage covers {policy_title} procedure code {pcode} ({pdesc}) subject to plan medical necessity criteria and laboratory billing guidelines."

    # Case 5: Covered services table
    if "covered services" in header.lower():
        srv = list(kv.keys())[0] if kv else "Services"
        desc = list(kv.values())[0] if kv else ""
        return f"Humana covers {srv} ({desc}) subject to medical necessity documentation and plan coverage guidelines."

    # General Table synthesis
    sentences = []
    for k, v in kv.items():
        if k and v:
            sentences.append(f"{k.capitalize()} is {v}")
    if sentences:
        return f"{header}: {'; '.join(sentences)}."
    
    return text

def classify_rule(text: str, content_type: str) -> Tuple[str, str]:
    """Returns (rule_category, audit_action)."""
    t_lower = text.lower()

    # 1. Bill Type Restriction
    if BILL_TYPE_RE.search(t_lower) and (DENIAL_RE.search(t_lower) or "require" in t_lower or "limit" in t_lower):
        return "BILL_TYPE_RESTRICTION", "DENY_LINE"

    # 2. Bundling / Unbundling
    if BUNDLING_RE.search(t_lower):
        if DENIAL_RE.search(t_lower) or "not separately" in t_lower or "inclusive" in t_lower:
            return "BUNDLING_INCLUSION", "DENY_LINE"
        return "BUNDLING_INCLUSION", "DENY_LINE"

    # 3. Frequency / Unit Limits
    if FREQUENCY_RE.search(t_lower):
        return "FREQUENCY_LIMIT", "DENY_EXCESS_UNITS"

    # 4. Modifier Prohibited / Informational Only
    if MOD_PROHIBIT_RE.search(t_lower) and MODIFIER_RE.search(t_lower):
        return "MODIFIER_PROHIBITED", "REDUCE_LINE_PAYMENT"

    # 5. Modifier Required
    if MODIFIER_RE.search(t_lower) and any(m in text for m in KNOWN_MODIFIERS_LIST):
        if "must be billed" in t_lower or "required" in t_lower or "append" in t_lower or "modifier" in t_lower:
            return "MODIFIER_REQUIRED", "DENY_MISSING_MODIFIER"

    # 6. Prior Authorization
    if AUTH_RE.search(t_lower):
        return "PRIOR_AUTHORIZATION", "DENY_NO_AUTH"

    # 7. Age Restriction (Strict regex with word boundaries)
    if AGE_RE.search(t_lower):
        return "AGE_RESTRICTION", "DENY_LINE"

    # 8. Denial / Non-Covered
    if DENIAL_RE.search(t_lower):
        return "DENIAL_PROHIBITION", "DENY_LINE"

    # 9. Conditional Reimbursement
    if REIMBURSEMENT_RE.search(t_lower) or ("covers" in t_lower and ("subject to" in t_lower or "criteria" in t_lower or "guidelines" in t_lower or "medical necessity" in t_lower)):
        return "CONDITIONAL_REIMBURSEMENT", "ADJUST_REIMBURSEMENT"

    return "POLICY_REQUIREMENT", "FLAG_SUSPEND_FOR_REVIEW"

def extract_modifiers_from_text(text: str) -> Tuple[List[str], List[str]]:
    """Returns (required_modifiers, prohibited_modifiers)."""
    req_mods = set()
    prohib_mods = set()
    t_lower = text.lower()

    # Search for 'modifier XX' pattern
    matches = re.findall(r'\bmodifier[s]?\s+([A-Z0-9]{2}(?:\s*,\s*[A-Z0-9]{2})*)', text, re.I)
    for m in matches:
        for single in re.split(r'[, ]+', m):
            single = single.strip().upper()
            if single in KNOWN_MODIFIERS_LIST:
                if "informational" in t_lower or "not allow" in t_lower or "not reimburs" in t_lower:
                    prohib_mods.add(single)
                else:
                    req_mods.add(single)

    # Check direct modifier mentions
    for m in KNOWN_MODIFIERS_LIST:
        if re.search(rf'\bmodifier\s+{m}\b', text, re.I) or re.search(rf'\b{m}\s+modifier\b', text, re.I):
            if "informational" in t_lower or "not allow" in t_lower or "do not reimburse" in t_lower:
                prohib_mods.add(m)
            else:
                req_mods.add(m)

    # Specific check for P1-P6 in anesthesia
    if "p1" in t_lower and "p6" in t_lower:
        prohib_mods.update(["P1", "P2", "P3", "P4", "P5", "P6"])

    # Ensure mutual exclusivity
    return sorted(list(req_mods - prohib_mods)), sorted(list(prohib_mods))

def extract_bill_types_from_text(text: str) -> List[str]:
    bt = set()
    matches = re.findall(r'\b(0[1-9][0-9][0-9A-Z])\b', text)
    for m in matches:
        bt.add(m)
    range_match = re.findall(r'(0[1-9][0-9][0-9A-Z])\s*[-–]\s*(0[1-9][0-9][0-9A-Z])', text)
    for start, end in range_match:
        bt.add(f"{start}-{end}")
    x_match = re.findall(r'\b(0[1-9][0-9]X)\b', text, re.I)
    for m in x_match:
        bt.add(m.upper())
    return sorted(list(bt))

def derive_claim_columns(rule_category: str, has_mods: bool, has_bt: bool, bclass: str) -> List[str]:
    """Generates complete list of database columns required to execute claims queries."""
    cols = [
        "CLAIM_ID", "LINE_NUM", "LINE_OF_BUSINESS", "CLAIM_TYPE", "PROC_CODE",
        "SERV_FROM_DATE", "SERV_TO_DATE", "UNITS_BILLED", "PAID_UNITS", "PAID_AMT", "LINE_STATUS"
    ]
    if bclass in ("Professional", "Both"):
        cols.append("REND_PROVIDER_NPI")
    if has_mods or "MODIFIER" in rule_category:
        cols.extend(["MODIFIER_1", "MODIFIER_2", "MODIFIER_3", "MODIFIER_4"])
    if has_bt or rule_category == "BILL_TYPE_RESTRICTION" or bclass in ("Institutional", "Both"):
        cols.extend(["BILL_TYPE", "REV_CODE"])
    if rule_category in ("BUNDLING_INCLUSION", "CONDITIONAL_REIMBURSEMENT"):
        cols.extend(["DIAG_CODE_1", "DIAG_CODE_2"])
    return list(dict.fromkeys(cols))

def generate_pseudo_sql(
    rule_id: str,
    rule_category: str,
    audit_action: str,
    primary_codes: List[str],
    secondary_codes: List[str],
    req_modifiers: List[str],
    prohib_modifiers: List[str],
    bill_types: List[str],
    billing_class: str,
    rule_statement: str
) -> str:
    """Generates standardized, syntactically clean pseudo-SQL for concept developers."""
    conditions = [
        "claim.line_of_business = 'MEDICARE_ADVANTAGE'"
    ]

    # 1. Billing Class Filter
    if billing_class == "Professional":
        conditions.append("claim.claim_type = 'PROFESSIONAL'")
    elif billing_class == "Institutional":
        conditions.append("claim.claim_type = 'INSTITUTIONAL'")

    # 2. Primary Procedure Codes
    if primary_codes:
        if len(primary_codes) == 1:
            conditions.append(f"claim_line.cpt_code = '{primary_codes[0]}'")
        elif len(primary_codes) <= 6:
            code_str = ", ".join(f"'{c}'" for c in primary_codes)
            conditions.append(f"claim_line.cpt_code IN ({code_str})")
        else:
            sample_str = ", ".join(f"'{c}'" for c in primary_codes[:4])
            conditions.append(f"claim_line.cpt_code IN ({sample_str}, ... /* {len(primary_codes)} total codes */)")

    # 3. Bill Types (UB-04 Facility Claims)
    if bill_types:
        bt_conds = []
        for bt in bill_types[:4]:
            if "-" in bt:
                s, e = bt.split("-")
                bt_conds.append(f"claim.bill_type BETWEEN '{s.strip()}' AND '{e.strip()}'")
            elif bt.endswith("X"):
                prefix = bt[:-1]
                bt_conds.append(f"claim.bill_type LIKE '{prefix}%'")
            else:
                bt_conds.append(f"claim.bill_type = '{bt}'")
        if bt_conds:
            conditions.append(f"({' OR '.join(bt_conds)})")

    # 4. Modifiers Required
    if req_modifiers:
        mod_list = ", ".join(f"'{m}'" for m in req_modifiers[:4])
        conditions.append(
            f"NOT (claim_line.modifier_1 IN ({mod_list}) OR "
            f"claim_line.modifier_2 IN ({mod_list}) OR "
            f"claim_line.modifier_3 IN ({mod_list}))"
        )

    # 5. Modifiers Prohibited
    if prohib_modifiers:
        p_list = ", ".join(f"'{m}'" for m in prohib_modifiers[:5])
        conditions.append(
            f"(claim_line.modifier_1 IN ({p_list}) OR "
            f"claim_line.modifier_2 IN ({p_list}) OR "
            f"claim_line.modifier_3 IN ({p_list}))"
        )

    # 6. Category Specific Logic
    if rule_category == "FREQUENCY_LIMIT":
        conditions.append("/* Unit Frequency Limit */\n  AND claim_line.units_billed > :max_allowed_units")
    elif rule_category == "BUNDLING_INCLUSION":
        conditions.append(
            "EXISTS (\n"
            "    SELECT 1 FROM claim_lines cl2\n"
            "    WHERE cl2.claim_id = claim.claim_id\n"
            "      AND cl2.line_num <> claim_line.line_num\n"
            "      AND cl2.cpt_code IN (:primary_inclusive_codes)\n"
            "  )"
        )
    elif rule_category == "BILL_TYPE_RESTRICTION" and "skin substitute" in rule_statement.lower():
        conditions.append(
            "NOT EXISTS (\n"
            "    SELECT 1 FROM claim_lines prod\n"
            "    WHERE prod.claim_id = claim.claim_id\n"
            "      AND prod.serv_from_date = claim_line.serv_from_date\n"
            "      AND prod.cpt_code LIKE 'Q4%'\n"
            "  )"
        )

    if not conditions:
        conditions.append("/* Filter criteria as detailed in rule_statement */\n  claim_line.line_status = 'PAID'")

    where_str = "\n  AND ".join(conditions)

    # Balanced, clean comment string
    clean_summary = re.sub(r'[\r\n]+', ' ', rule_statement.split('.')[0]).strip()
    clean_summary = re.sub(r'\s*\([^)]*$', '', clean_summary)
    if len(clean_summary) > 100:
        clean_summary = clean_summary[:97] + "..."
    # Balance open/close parentheses
    o_cnt = clean_summary.count("(")
    c_cnt = clean_summary.count(")")
    if o_cnt > c_cnt:
        clean_summary += ")" * (o_cnt - c_cnt)
    elif c_cnt > o_cnt:
        clean_summary = "(" * (c_cnt - o_cnt) + clean_summary

    sql = (
        f"-- Query Spec for Rule {rule_id} [{audit_action}]\n"
        f"-- Policy Requirement: {clean_summary}\n"
        f"SELECT\n"
        f"    claim.claim_id,\n"
        f"    claim_line.line_num,\n"
        f"    claim.claim_type,\n"
        f"    claim_line.cpt_code,\n"
        f"    claim_line.serv_from_date,\n"
        f"    claim_line.units_billed,\n"
        f"    claim_line.paid_amt,\n"
        f"    '{audit_action}' AS audit_recommendation\n"
        f"FROM claims claim\n"
        f"JOIN claim_lines claim_line ON claim.claim_id = claim_line.claim_id\n"
        f"WHERE {where_str};"
    )
    return sql

def generate_dev_notes(rule_id: str, rule_category: str, bclass: str, has_mods: bool, has_bt: bool) -> str:
    notes = ["Medicare Advantage claims edit. Filter: claim.line_of_business = 'MEDICARE_ADVANTAGE'."]
    if has_bt or rule_category == "BILL_TYPE_RESTRICTION":
        notes.append("Institutional claim check: verify 3-digit UB-04 Type of Bill matches facility scope.")
    if has_mods or "MODIFIER" in rule_category:
        notes.append("Modifier evaluation: scan modifier fields 1 through 4 across primary and secondary lines.")
    if rule_category == "FREQUENCY_LIMIT":
        notes.append("Window function: partition by (member_id, serv_from_date) or calendar year to compute cumulative units.")
    if rule_category == "BUNDLING_INCLUSION":
        notes.append("Cross-line comparison: ensure both primary and bundled service are present on same DOS.")
    return " ".join(notes)

def run_pipeline():
    print("=" * 80)
    print("HUMANA BILLING RULE GENERATION PIPELINE (FOR CONCEPT DEVELOPMENT)")
    print(f"Source Extraction Rules: {INPUT_JSON}")
    print("=" * 80)

    if not INPUT_JSON.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_JSON}")

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        policies = json.load(f)

    print(f"Loaded {len(policies)} policy objects.")

    rules_list = []
    code_index_records = []
    category_counts = Counter()
    lob_counts = Counter()
    action_counts = Counter()
    distinct_codes_counter = Counter()

    medicare_policies_processed = 0
    excluded_non_medicare_policies = 0

    for pol_idx, policy in enumerate(policies, 1):
        raw_lob = policy.get("line_of_business") or ""
        # STRICT MEDICARE PLANS CONSTRAINT: Exclude all non-Medicare lines of business
        if "medicare" not in raw_lob.lower():
            excluded_non_medicare_policies += 1
            continue

        medicare_policies_processed += 1
        title = clean_text(policy.get("policy_title", f"Policy_{pol_idx}"))
        pnum = policy.get("policy_number") or ""
        lob = "Medicare Advantage"
        bclass = policy.get("billing_class") or "Both"
        src_pdf = policy.get("source_pdf") or ""
        mapped_cpts = policy.get("mapped_cpt_codes", [])

        # Policy-level CPT pool (clean format only)
        policy_cpt_pool = [
            m["code"] for m in mapped_cpts
            if re.match(r'^[0-9]{5}$|^[A-Z][0-9]{4}$|^[0-9]{4}[A-Z]$', m.get("code", ""))
        ] if mapped_cpts else []

        # Determine policy slug
        slug = POLICY_SLUG_MAP.get(title)
        if not slug:
            words = [w for w in re.split(r'[^A-Za-z0-9]', title) if w and len(w) > 1 and w.lower() not in ("for", "and", "the", "in", "of", "to")]
            slug = "".join(w[0].upper() for w in words[:4]) if words else f"P{pol_idx:02d}"

        extracted_content = policy.get("extracted_content", [])
        policy_rule_seq = 0

        for block in extracted_content:
            raw_text = block.get("content", "")
            text = clean_text(raw_text)
            if len(text) < 30:
                continue

            # Skip Medicaid-specific blocks within multi-LOB policies
            t_low = text.lower()
            if any(w in t_low for w in ["medicaid only", "applies only to medicaid", "kentucky medicaid", "south carolina medicaid", "florida medicaid"]):
                continue

            # Skip administrative disclaimers or document revision history
            title_lower = (block.get("title") or "").lower()
            if any(ign in title_lower for ign in ["revision history", "references", "disclaimer", "publications", "contacts", "inquiries"]):
                continue

            # Transform raw table fragments into natural language rule statements
            synthesized_stmt = synthesize_table_statement(text, title)

            ctype = block.get("content_type", "paragraph")
            rule_id_spec = block.get("rule_identifier")

            # Classify Rule
            rule_cat, audit_action = classify_rule(synthesized_stmt, ctype)

            # Skip pure general statements that don't state any billing rule or condition
            if rule_cat == "POLICY_REQUIREMENT":
                if not any(k in synthesized_stmt.lower() for k in ["must", "require", "bill", "limit", "eligible", "reimburse", "allow", "modifier"]):
                    continue

            policy_rule_seq += 1
            if rule_id_spec:
                rule_id = f"HUM-SC-{rule_id_spec}"
            elif slug == "MOD" and any(m in synthesized_stmt for m in KNOWN_MODIFIERS_LIST):
                matched_mod = next(m for m in KNOWN_MODIFIERS_LIST if m in synthesized_stmt)
                rule_id = f"HUM-MOD-M{matched_mod}-R{policy_rule_seq:02d}"
            else:
                rule_id = f"HUM-{slug}-R{policy_rule_seq:03d}"

            # Filter valid CPT/HCPCS codes strictly (discard X12, B12, ICD diagnosis codes)
            raw_block_codes = block.get("codes", [])
            valid_block_codes = [
                c for c in raw_block_codes
                if re.match(r'^[0-9]{5}$|^[A-Z][0-9]{4}$|^[0-9]{4}[A-Z]$', c) and c not in ("X12", "B12")
            ]
            primary_codes = valid_block_codes if valid_block_codes else policy_cpt_pool[:8]

            # Detect modifiers and bill types
            req_mods, prohib_mods = extract_modifiers_from_text(synthesized_stmt)
            bill_types = extract_bill_types_from_text(synthesized_stmt)

            # Clean rule statement prefix
            clean_stmt = synthesized_stmt
            if clean_stmt.startswith("[Rule ") and "]" in clean_stmt:
                clean_stmt = clean_stmt[clean_stmt.find("]")+1:].strip()
            if "(Applies to:" in clean_stmt:
                clean_stmt = clean_stmt[:clean_stmt.find("(Applies to:")].strip()

            # Skip empty subheaders like "Humana Modifier 50: Modifier 50 :"
            if len(clean_stmt) < 35 or re.match(r'^(?:Humana\s+)?Modifier\s+[A-Z0-9]{2}\s*:\s*$', clean_stmt, re.I):
                continue
            if len(clean_stmt) < 40:
                clean_stmt = f"Humana {title}: {clean_stmt}"

            # Conditions
            cond_match = re.search(r'\b(if [^\.;]+|when [^\.;]+|provided that [^\.;]+|under the following[^\.;]+)', clean_stmt, re.I)
            conditions_text = cond_match.group(0).strip() if cond_match else f"Applicable to {title} services under {lob}."

            # Exceptions / Overrides
            exc_match = re.search(r'\b(unless[^\.;]+|except[^\.;]+|with the exception of[^\.;]+|override[^\.;]+)', clean_stmt, re.I)
            exceptions_text = exc_match.group(0).strip() if exc_match else "Standard payer clinical claim editing rules apply; no automated bypass."

            # Database Columns Required (complete set)
            claim_cols = derive_claim_columns(rule_cat, bool(req_mods or prohib_mods), bool(bill_types), bclass)

            # Pseudo-SQL Query
            pseudo_sql = generate_pseudo_sql(
                rule_id=rule_id,
                rule_category=rule_cat,
                audit_action=audit_action,
                primary_codes=primary_codes,
                secondary_codes=[],
                req_modifiers=req_mods,
                prohib_modifiers=prohib_mods,
                bill_types=bill_types,
                billing_class=bclass,
                rule_statement=clean_stmt
            )

            dev_notes = generate_dev_notes(rule_id, rule_cat, bclass, bool(req_mods or prohib_mods), bool(bill_types))

            source_page = block.get("page_number") or block.get("page_start") or 1
            confidence = 0.98 if (primary_codes and rule_cat != "POLICY_REQUIREMENT") else 0.92

            rule_entry = {
                "rule_id": rule_id,
                "policy_title": title,
                "policy_number": pnum if pnum else "Humana Payment Policy",
                "line_of_business": lob,
                "billing_class": bclass,
                "rule_category": rule_cat,
                "audit_action": audit_action,
                "rule_statement": clean_stmt,
                "primary_cpt_codes": ", ".join(primary_codes) if primary_codes else "See Policy Scope",
                "total_primary_codes": len(primary_codes),
                "modifiers_required": ", ".join(req_mods) if req_mods else "None",
                "modifiers_prohibited": ", ".join(prohib_mods) if prohib_mods else "None",
                "bill_types": ", ".join(bill_types) if bill_types else "All Applicable",
                "trigger_conditions": conditions_text,
                "exceptions_overrides": exceptions_text,
                "claim_columns_required": ", ".join(claim_cols),
                "concept_dev_notes": dev_notes,
                "query_logic_pseudo_sql": pseudo_sql,
                "source_pdf": src_pdf,
                "source_page": source_page,
                "confidence_score": confidence
            }
            rules_list.append(rule_entry)
            category_counts[rule_cat] += 1
            lob_counts[lob] += 1
            action_counts[audit_action] += 1

            # Populate Code Index
            for c in primary_codes:
                if (len(c) == 5 and c.isdigit()) or re.match(r'^[A-Z][0-9]{4}$|^[0-9]{4}[A-Z]$', c):
                    code_index_records.append({
                        "cpt_hcpcs_code": c,
                        "rule_id": rule_id,
                        "policy_title": title,
                        "rule_category": rule_cat,
                        "audit_action": audit_action,
                        "rule_statement_snippet": clean_stmt[:140] + "..." if len(clean_stmt) > 140 else clean_stmt
                    })
                    distinct_codes_counter[c] += 1

    print(f"\nSuccessfully generated {len(rules_list)} structured rules across {len(policies)} policies.")
    print(f"Indexed {len(code_index_records)} code-rule linkages across {len(distinct_codes_counter)} distinct CPT/HCPCS codes.")

    # Convert to DataFrames
    df_rules = pd.DataFrame(rules_list)
    df_code_index = pd.DataFrame(code_index_records).drop_duplicates()

    # Create Query_Logic dedicated dataframe
    df_query_logic = df_rules[[
        "rule_id", "policy_title", "rule_category", "audit_action",
        "primary_cpt_codes", "modifiers_required", "modifiers_prohibited",
        "bill_types", "claim_columns_required", "concept_dev_notes",
        "query_logic_pseudo_sql", "source_pdf", "source_page"
    ]].copy()

    # Create Summary DataFrame
    summary_data = [
        {"Section": "Corpus Scope", "Metric": "Target Line of Business", "Value": "Medicare Plans Only (Medicare Advantage / Part B)"},
        {"Section": "Corpus Metrics", "Metric": "Total Medicare Policies Processed", "Value": medicare_policies_processed},
        {"Section": "Corpus Metrics", "Metric": "Excluded Non-Medicare Policies", "Value": excluded_non_medicare_policies},
        {"Section": "Corpus Metrics", "Metric": "Total Clear-Cut Medicare Billing Rules", "Value": len(rules_list)},
        {"Section": "Corpus Metrics", "Metric": "Distinct CPT/HCPCS Codes Covered", "Value": len(distinct_codes_counter)},
        {"Section": "Corpus Metrics", "Metric": "Total Code-to-Rule Associative Linkages", "Value": len(df_code_index)},
        {"Section": "Corpus Metrics", "Metric": "Average Rules Generated per Policy", "Value": round(len(rules_list) / max(medicare_policies_processed, 1), 2)},
    ]
    for cat, count in category_counts.most_common():
        summary_data.append({"Section": "Rule Categories", "Metric": f"Category: {cat}", "Value": count})
    for act, count in action_counts.most_common():
        summary_data.append({"Section": "Audit Actions", "Metric": f"Action: {act}", "Value": count})
    for lob_name, count in lob_counts.most_common():
        summary_data.append({"Section": "Lines of Business", "Metric": f"LOB: {lob_name}", "Value": count})

    df_summary = pd.DataFrame(summary_data)

    # Save Excel Workbook with 4 Sheets
    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
        df_rules.to_excel(writer, sheet_name="Rules", index=False)
        df_code_index.to_excel(writer, sheet_name="Code_Index", index=False)
        df_query_logic.to_excel(writer, sheet_name="Query_Logic", index=False)
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
    print(f"[PASS] Successfully wrote Excel Workbook: {OUTPUT_XLSX}")

    with pd.ExcelWriter(MIRROR_XLSX, engine="openpyxl") as writer:
        df_rules.to_excel(writer, sheet_name="Rules", index=False)
        df_code_index.to_excel(writer, sheet_name="Code_Index", index=False)
        df_query_logic.to_excel(writer, sheet_name="Query_Logic", index=False)
        df_summary.to_excel(writer, sheet_name="Summary", index=False)
    print(f"[PASS] Successfully wrote Mirror Excel Workbook: {MIRROR_XLSX}")

    # Save JSON Outputs
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rules_list, f, indent=2)
    print(f"[PASS] Successfully wrote JSON Rules: {OUTPUT_JSON}")

    with open(MIRROR_JSON, "w", encoding="utf-8") as f:
        json.dump(rules_list, f, indent=2)
    print(f"[PASS] Successfully wrote Mirror JSON Rules: {MIRROR_JSON}")

    print("\n" + "=" * 80)
    print("RULE GENERATION SUMMARY REPORT")
    print("=" * 80)
    print(f"Total Rules Generated:           {len(rules_list)}")
    print(f"Distinct CPT/HCPCS Codes:        {len(distinct_codes_counter)}")
    print(f"\nRules by Category:")
    for cat, cnt in category_counts.most_common():
        print(f"  - {cat:28s}: {cnt:4d} ({cnt/len(rules_list)*100:.1f}%)")
    print(f"\nRules by Audit Action:")
    for act, cnt in action_counts.most_common():
        print(f"  - {act:28s}: {cnt:4d} ({cnt/len(rules_list)*100:.1f}%)")
    print("=" * 80)

if __name__ == "__main__":
    run_pipeline()
