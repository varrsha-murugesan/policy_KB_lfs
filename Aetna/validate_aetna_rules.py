#!/usr/bin/env python3
"""
validate_aetna_rules.py
=======================
Comprehensive Validation Engine for Aetna Generated Billing Rules.
Evaluates all generated rules across:
  1. Interpretability (grammar, clarity, deontic direction, encoding cleanliness)
  2. Accuracy (code syntax, modifier syntax, category-action alignment, SQL balance)
  3. Grammatical Appropriateness (capitalization, terminal punctuation, balanced quotes & brackets)
Emits: `aetna_rules_validation_report.json`
"""

import os
import sys
import re
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(r"c:\Payer Policy\Aetna")
RULES_JSON = BASE_DIR / "billing_rules.json"
REPORT_JSON = BASE_DIR / "aetna_rules_validation_report.json"

EXPECTED_ACTIONS = {
    "FREQUENCY_LIMIT": ["DENY_EXCESS_UNITS"],
    "MODIFIER_REQUIRED": ["DENY_MISSING_MODIFIER"],
    "MODIFIER_PROHIBITED": ["REDUCE_LINE_PAYMENT", "DENY_LINE"],
    "DENIAL_PROHIBITION": ["DENY_LINE"],
    "BILL_TYPE_RESTRICTION": ["DENY_LINE"],
    "BUNDLING_INCLUSION": ["DENY_LINE"],
    "PLACE_OF_SERVICE_RESTRICTION": ["DENY_LINE"],
    "AGE_RESTRICTION": ["DENY_LINE"],
    "PRIOR_AUTHORIZATION": ["DENY_NO_AUTH"],
    "CONDITIONAL_REIMBURSEMENT": ["ADJUST_REIMBURSEMENT", "FLAG_SUSPEND_FOR_REVIEW"],
    "DOCUMENTATION_REQUIREMENT": ["FLAG_SUSPEND_FOR_REVIEW"],
    "POLICY_REQUIREMENT": ["FLAG_SUSPEND_FOR_REVIEW"]
}

DEONTIC_KEYWORDS = [
    "reimburse", "reimbursement", "reimbursable", "non-reimbursable", "allow", "allowed",
    "limit", "limited", "limitation", "require", "required", "requirement", "must",
    "bill", "billed", "billing", "modifier", "covered", "coverage", "charge", "charges",
    "pay", "payable", "payment", "denied", "deny", "denial", "prohibit", "prohibited",
    "bundled", "inclusive", "prior authorization", "session", "unit", "units",
    "criteria", "guideline", "guidelines", "documentation", "medical necessity", "eligible",
    "place of service", "pos", "support", "supported", "reported", "appropriate", "indicat"
]

def run_validation():
    print("=" * 80)
    print("AETNA BILLING RULES COMPREHENSIVE VALIDATION SUITE")
    print(f"Target: {RULES_JSON}")
    print("=" * 80)

    if not RULES_JSON.exists():
        raise FileNotFoundError(f"Missing rules file: {RULES_JSON}")

    with open(RULES_JSON, "r", encoding="utf-8") as f:
        rules = json.load(f)

    total_rules = len(rules)
    print(f"Loaded {total_rules} rules.\n")

    # 1. Interpretability Audit
    short_statements = []
    dangling_fragments = []
    missing_deontic = []
    encoding_artifacts = []

    # 2. Accuracy Audit
    invalid_code_formats = []
    distinct_codes = set()
    invalid_modifier_formats = []
    modifier_overlaps = []
    sql_syntax_issues = []

    # 3. Grammatical Appropriateness Audit
    uncapitalized_statements = []
    missing_terminal_punct = []
    unbalanced_parentheses = []
    unbalanced_quotes = []

    # 4. Consistency & Alignment
    category_action_mismatches = []
    missing_claim_columns = []
    action_counts = {}
    category_counts = {}

    for r in rules:
        rid = r["rule_id"]
        stmt = r["rule_statement"]
        cat = r["rule_category"]
        act = r["audit_action"]
        codes_str = r["primary_cpt_codes"]
        req_mods_str = r["modifiers_required"]
        prohib_mods_str = r["modifiers_prohibited"]
        cols_str = r["claim_columns_required"]
        sql = r["query_logic_pseudo_sql"]

        action_counts[act] = action_counts.get(act, 0) + 1
        category_counts[cat] = category_counts.get(cat, 0) + 1

        # ----------------------------------------------------
        # 1. Interpretability
        # ----------------------------------------------------
        if len(stmt) < 35:
            short_statements.append({"rule_id": rid, "length": len(stmt), "statement": stmt})
        if re.search(r'\b(when|includes|for|such as|on the same)\s*[\.\;]?$', stmt, re.I):
            dangling_fragments.append({"rule_id": rid, "statement": stmt})
        if not any(dk in stmt.lower() for dk in DEONTIC_KEYWORDS):
            missing_deontic.append({"rule_id": rid, "statement": stmt})
        if "\ufffd" in stmt or "\ufffd" in sql:
            encoding_artifacts.append({"rule_id": rid, "issue": "Contains unicode replacement character"})

        # ----------------------------------------------------
        # 2. Accuracy
        # ----------------------------------------------------
        if codes_str != "See Policy Scope":
            for c in [x.strip() for x in codes_str.split(",") if x.strip()]:
                distinct_codes.add(c)
                if not re.match(r'^[0-9]{5}$|^[A-Z][0-9]{4}$|^[0-9]{4}[A-Z]$', c):
                    invalid_code_formats.append({"rule_id": rid, "code": c})

        req_set = set([m.strip() for m in req_mods_str.split(",") if m.strip() != "None"])
        prohib_set = set([m.strip() for m in prohib_mods_str.split(",") if m.strip() != "None"])

        for m in req_set | prohib_set:
            if not re.match(r'^[A-Z0-9]{2}$', m):
                invalid_modifier_formats.append({"rule_id": rid, "modifier": m})

        overlap = req_set & prohib_set
        if overlap:
            modifier_overlaps.append({"rule_id": rid, "overlap": list(overlap)})

        # SQL check
        if sql.count("(") != sql.count(")"):
            sql_syntax_issues.append({"rule_id": rid, "issue": "Unbalanced parentheses in pseudo-SQL"})
        if not sql.startswith("-- Query Spec"):
            sql_syntax_issues.append({"rule_id": rid, "issue": "Missing query header comment"})
        if "SELECT" not in sql or "FROM" not in sql or "WHERE" not in sql:
            sql_syntax_issues.append({"rule_id": rid, "issue": "Malformed SQL query structure"})

        # ----------------------------------------------------
        # 3. Grammatical Appropriateness
        # ----------------------------------------------------
        if not stmt[0].isupper() and not stmt[0].isdigit():
            uncapitalized_statements.append({"rule_id": rid, "statement": stmt[:60]})
        if not stmt.endswith((".", ";")):
            missing_terminal_punct.append({"rule_id": rid, "statement": stmt[-30:]})
        if stmt.count("(") != stmt.count(")"):
            unbalanced_parentheses.append({"rule_id": rid, "statement": stmt})
        if stmt.count('"') % 2 != 0:
            unbalanced_quotes.append({"rule_id": rid, "statement": stmt})

        # ----------------------------------------------------
        # 4. Alignment
        # ----------------------------------------------------
        allowed_actions = EXPECTED_ACTIONS.get(cat, [])
        if act not in allowed_actions:
            category_action_mismatches.append({"rule_id": rid, "category": cat, "action": act})

        req_cols = [c.strip() for c in cols_str.split(",")]
        for core_col in ["CLAIM_ID", "LINE_NUM", "LINE_OF_BUSINESS", "PROC_CODE", "PAID_AMT"]:
            if core_col not in req_cols:
                missing_claim_columns.append({"rule_id": rid, "missing_col": core_col})

    # Summary Metrics
    interpretability_score = 100.0 * (total_rules - len(short_statements) - len(dangling_fragments) - len(missing_deontic) - len(encoding_artifacts)) / total_rules
    accuracy_score = 100.0 * (total_rules - len(invalid_code_formats) - len(invalid_modifier_formats) - len(modifier_overlaps) - len(sql_syntax_issues)) / total_rules
    grammar_score = 100.0 * (total_rules - len(uncapitalized_statements) - len(missing_terminal_punct) - len(unbalanced_parentheses) - len(unbalanced_quotes)) / total_rules
    overall_score = round((interpretability_score + accuracy_score + grammar_score) / 3.0, 2)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_rules_evaluated": total_rules,
        "distinct_cpt_codes_covered": len(distinct_codes),
        "scores": {
            "interpretability_score": round(interpretability_score, 2),
            "accuracy_score": round(accuracy_score, 2),
            "grammar_score": round(grammar_score, 2),
            "overall_validation_score": overall_score
        },
        "interpretability_audit": {
            "short_statements_count": len(short_statements),
            "short_statements": short_statements,
            "dangling_fragments_count": len(dangling_fragments),
            "dangling_fragments": dangling_fragments,
            "missing_deontic_count": len(missing_deontic),
            "missing_deontic": missing_deontic,
            "encoding_artifacts_count": len(encoding_artifacts),
            "encoding_artifacts": encoding_artifacts
        },
        "accuracy_audit": {
            "invalid_code_formats_count": len(invalid_code_formats),
            "invalid_code_formats": invalid_code_formats,
            "invalid_modifier_formats_count": len(invalid_modifier_formats),
            "invalid_modifier_formats": invalid_modifier_formats,
            "modifier_overlaps_count": len(modifier_overlaps),
            "modifier_overlaps": modifier_overlaps,
            "sql_syntax_issues_count": len(sql_syntax_issues),
            "sql_syntax_issues": sql_syntax_issues
        },
        "grammar_audit": {
            "uncapitalized_statements_count": len(uncapitalized_statements),
            "uncapitalized_statements": uncapitalized_statements,
            "missing_terminal_punct_count": len(missing_terminal_punct),
            "missing_terminal_punct": missing_terminal_punct,
            "unbalanced_parentheses_count": len(unbalanced_parentheses),
            "unbalanced_parentheses": unbalanced_parentheses,
            "unbalanced_quotes_count": len(unbalanced_quotes),
            "unbalanced_quotes": unbalanced_quotes
        },
        "alignment_audit": {
            "category_action_mismatches_count": len(category_action_mismatches),
            "category_action_mismatches": category_action_mismatches,
            "missing_claim_columns_count": len(missing_claim_columns)
        },
        "rule_distribution": {
            "categories": category_counts,
            "actions": action_counts
        }
    }

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("VALIDATION RESULTS BREAKDOWN:")
    print("-" * 50)
    print(f"1. Interpretability Score: {interpretability_score:.2f}%")
    print(f"   - Short statements (<35 char): {len(short_statements)}")
    print(f"   - Dangling fragments:          {len(dangling_fragments)}")
    print(f"   - Missing deontic keywords:    {len(missing_deontic)}")
    print(f"   - Encoding artifacts:          {len(encoding_artifacts)}")
    print(f"\n2. Accuracy Score:         {accuracy_score:.2f}%")
    print(f"   - Invalid code formats:        {len(invalid_code_formats)}")
    print(f"   - Invalid modifier formats:    {len(invalid_modifier_formats)}")
    print(f"   - Modifier overlaps:           {len(modifier_overlaps)}")
    print(f"   - Pseudo-SQL syntax issues:    {len(sql_syntax_issues)}")
    print(f"\n3. Grammar Score:          {grammar_score:.2f}%")
    print(f"   - Uncapitalized statements:    {len(uncapitalized_statements)}")
    print(f"   - Missing terminal punct:      {len(missing_terminal_punct)}")
    print(f"   - Unbalanced parentheses:      {len(unbalanced_parentheses)}")
    print(f"   - Unbalanced quotes:           {len(unbalanced_quotes)}")
    print(f"\n4. Alignment Audit:")
    print(f"   - Category/Action mismatches:  {len(category_action_mismatches)}")
    print(f"   - Missing claim columns:       {len(missing_claim_columns)}")
    print("-" * 50)
    print(f"OVERALL VALIDATION SCORE: {overall_score:.2f}%")
    print("=" * 80)
    print(f"[PASS] Validation Report written to: {REPORT_JSON}")

    if overall_score >= 98.0:
        print("[SUCCESS] All generated rules meet high clinical and engineering standards!")
    else:
        print("[WARNING] Some rules require refinement.")

if __name__ == "__main__":
    run_validation()
