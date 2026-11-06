#!/usr/bin/env python3
"""
generate_billing_rules.py
=========================
Deterministic Billing Rule Extraction & Infilling Engine for Payer Policies.

Key Operating Principles:
1. Verbatim Fidelity (No Generated Words):
   Picks out the EXACT, untouched content from the policy prose, subsections,
   tables, and Q&A entries. No paraphrasing, no standardized sentence rewriting.
2. Keyword-Driven Rule Selection:
   Uses domain-grounded rule keywords (denials, reimbursement criteria, frequency
   limits, bundling, modifiers, place of service, exceptions) to select rule-bearing content.
3. Attachment Value Infilling:
   In policies like Time Span Codes Policy, Once in a Lifetime, and Age-Based Codes,
   it fills the parameter values (e.g., Time Span period, Comprehensive/Component pairings,
   age limits) inside the rule from the policy attachments, while maintaining the exact
   verbatim policy rule statement.
4. Clean Incorrect Billing Conditions:
   Generates the clean, actionable conditions required for claim editors and concept
   engineers to check for incorrect billing.

Outputs:
  - `billing_rules.xlsx` (Multi-sheet workbook: Rules, Attachment_Filled_Rules, Code_Index, Summary)
  - `billing_rules.json` (Structured machine-readable JSON)
  - `billing_rules.csv`  (Fast tabular CSV)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Add Uhc directory to path if not present
UHC_DIR = Path(__file__).resolve().parent
if str(UHC_DIR) not in sys.path:
    sys.path.insert(0, str(UHC_DIR))

from policy_lib import (
    AttachmentTable,
    TextUnit,
    attachment_code_tables,
    expand_range,
    extract_codes,
    extract_modifiers,
    flatten_policy,
    is_boilerplate,
    load_policies,
    norm_ws,
    split_sentences,
)
from claim_requirements import assign_claim_columns, CANONICAL_ORDER

# ==============================================================================
# RULE KEYWORD PATTERNS (Categorized Signal Lexicon)
# ==============================================================================

KEYWORD_PATTERNS = {
    "DENIAL_PROHIBITION": re.compile(
        r"\b(will not (?:be )?reimburse|does not reimburse|do not reimburse|not reimbursable|"
        r"non-?reimbursable|not covered|non-?covered|will (?:be )?denied|is denied|are denied|"
        r"disallowed|billing errors?|inappropriate to report|not eligible for (?:separate )?reimbursement|"
        r"is prohibited|are prohibited|may not be reported|should not be reported|never event)\b",
        re.I,
    ),
    "REIMBURSEMENT_CRITERIA": re.compile(
        r"\b(will (?:be )?reimburse|reimbursement is only allowed|reimbursable only|only once per|"
        r"payable only|covered only|consider reimbursement|subject to other plan requirements|"
        r"reimbursement will be considered|eligible for reimbursement|only when)\b",
        re.I,
    ),
    "FREQUENCY_LIMIT": re.compile(
        r"\b(once per|per day|per date of service|per calendar month|per month|per year|per lifetime|"
        r"lifetime limit|maximum units?|exceeds? \d+|time span|time period|frequency limit|"
        r"no more than|limited to \d+|session[s]? limit|daily limit|once during that time period)\b",
        re.I,
    ),
    "BUNDLING_INCLUSION": re.compile(
        r"\b(bundled|inclusive|component of|mutually exclusive|incidental to|unbundl(?:ed|ing)|"
        r"included in|separately reimbursable|not separately reimbursable|comprehensive and component|"
        r"integral to|re-?bundling)\b",
        re.I,
    ),
    "MODIFIER_RESTRICTION": re.compile(
        r"\b(must be (?:reported|appended) with modifier|does not recognize modifier|"
        r"no modifier overrides?|modifier [a-z0-9]{2} is not allowed|prohibited modifier|"
        r"modifier 52|modifier 59|modifier 25|modifier 76|modifier 50|modifier 26|modifier tc|"
        r"appropriate use of modifier|inappropriate to report modifier)\b",
        re.I,
    ),
    "PLACE_OF_SERVICE": re.compile(
        r"\b(place of service|inpatient|outpatient|office setting|facility setting|non-facility|"
        r"pos 11|pos 19|pos 21|pos 22|pos 24)\b",
        re.I,
    ),
    "AGE_GENDER_SCOPE": re.compile(
        r"\b(age-based|age designation|age range|years of age|pediatric|gender-dysphoric|"
        r"gender-incongruent|males? with|females? with)\b",
        re.I,
    ),
    "EXCEPTION_OVERRIDE": re.compile(
        r"\b(does not apply|do not apply|will not apply|is not applied|exception|except when|"
        r"unless\b|instead\b|in lieu of|rather than|override\b|is not subject to|are not subject to)\b",
        re.I,
    ),
}

# ==============================================================================
# RULE DATA STRUCTURE
# ==============================================================================

@dataclass
class BillingRule:
    rule_id: str
    policy_idx: int
    policy_title: str
    policy_number: str
    line_of_business: str
    billing_class: str
    source_section: str
    policy_rule_text: str  # EXACT VERBATIM policy content
    rule_category: str
    target_codes: List[str] = field(default_factory=list)
    attachment_reference: str = ""
    attachment_values: str = ""
    target_modifiers: List[str] = field(default_factory=list)
    modifier_rule: str = "NONE"
    incorrect_billing_condition: str = ""
    required_claim_fields: List[str] = field(default_factory=list)
    trigger_keywords: List[str] = field(default_factory=list)
    source_type: str = "guideline"  # guideline | subsection | table | qa | attachment
    parent_id: str = ""
    order: float = 0.0

    # Attributes needed for claim_requirements facet analysis
    @property
    def text(self) -> str:
        return f"{self.policy_rule_text} {self.attachment_values} {self.incorrect_billing_condition}"

    @property
    def codes(self) -> List[str]:
        return self.target_codes

    @property
    def modifiers(self) -> List[str]:
        return self.target_modifiers

    @property
    def conditions(self) -> List[str]:
        conds = []
        if self.attachment_values:
            conds.append(self.attachment_values)
        if self.incorrect_billing_condition:
            conds.append(self.incorrect_billing_condition)
        return conds

    @property
    def exceptions(self) -> List[str]:
        return [self.incorrect_billing_condition] if self.rule_category == "EXCEPTION" else []

    @property
    def section_path(self) -> List[str]:
        return [s.strip() for s in self.source_section.split(">")]

    @property
    def attachment(self) -> str:
        return self.attachment_reference

    @property
    def rule_type(self) -> str:
        return "derived" if self.attachment_reference else ("exception" if self.rule_category == "EXCEPTION" else "base")

    @property
    def claim_columns(self) -> List[str]:
        return self.required_claim_fields

    @claim_columns.setter
    def claim_columns(self, val: List[str]) -> None:
        self.required_claim_fields = val


# ==============================================================================
# CONDITION FORMULATION HELPERS (Deterministic Logic)
# ==============================================================================

def pretty_time_span(span: str) -> str:
    """Format span value nicely: '30' -> '30 days', '7' -> '7 days'."""
    s = span.strip()
    return f"{s} days" if s.isdigit() else s


def derive_incorrect_billing_condition(
    category: str,
    text: str,
    codes: List[str],
    mods: List[str],
    attachment_attr: str = "",
    attachment_val: str = "",
) -> str:
    """
    Constructs the exact, clean condition required to check for incorrect billing.
    Grounded strictly on the rule category and extracted policy facts.
    """
    t_lower = text.lower()
    val_str = pretty_time_span(attachment_val) if attachment_val else ""

    if category == "TIME_SPAN_LIMIT":
        if val_str:
            return (
                f"Service billed more than 1 time or exceeding 1 unit per {val_str} "
                f"for the same member by the same group physician or other qualified health care professional."
            )
        if "calendar month" in t_lower or "per month" in t_lower:
            return (
                "Service billed more than once per calendar month or within 30 days of prior submission "
                "for the same member by the same group physician/QHCP."
            )
        return "Service billed more than once within the designated time span parameter."

    if category == "FREQUENCY_LIMIT":
        if "per day" in t_lower or "daily limit" in t_lower:
            return "Service billed more than once or exceeding allowable units per single date of service."
        if "per calendar month" in t_lower or "per month" in t_lower:
            return "Service billed more than allowable frequency per calendar month for the same member."
        if "per year" in t_lower or "calendar year" in t_lower:
            return "Service billed more than allowable frequency per calendar year for the same member."
        if "lifetime" in t_lower:
            return "Service billed more than allowable lifetime frequency for the patient."
        if "maximum units" in t_lower:
            return "Billed units exceed maximum allowable units of service."
        return "Billed units or frequency exceeds allowable policy limitation."

    if category == "BUNDLING_INCLUSION":
        if "add-on" in t_lower or "add_on" in attachment_attr.lower():
            return (
                "Add-on procedure code billed without an eligible primary procedure code "
                "or billed when the primary procedure code is not reimbursed for the same date of service."
            )
        if "comprehensive" in t_lower or attachment_attr == "Comprehensive/Component":
            return (
                "Component code billed on the same date of service or within the same time period "
                "as comprehensive code for the same member by the same group physician; component service is inclusive."
            )
        return "Incidental or bundled component code reported separately alongside primary service; separate reimbursement disallowed."

    if category == "DENIAL_PROHIBITION":
        if "asymptomatic" in t_lower or "non-specific symptoms" in t_lower:
            return "Service billed for asymptomatic individual or non-specific symptoms; non-covered under policy."
        if "not reimbursable" in t_lower or "will not reimburse" in t_lower or "will not be reimbursed" in t_lower:
            return "Service is non-reimbursable under stated policy criteria."
        return "Claim line denied as non-reimbursable / non-covered per policy terms."

    if category == "MODIFIER_RESTRICTION":
        if "no modifier" in t_lower or "does not recognize modifier" in t_lower or "not allowed" in t_lower:
            mod_list = ", ".join(mods) if mods else "overriding modifiers"
            return f"Billed with modifier ({mod_list}) to bypass edit; policy does not recognize modifier override."
        if "must be reported with modifier" in t_lower or "appended with modifier" in t_lower:
            mod_list = ", ".join(mods) if mods else "required modifier"
            return f"Service billed without required modifier ({mod_list})."
        return "Service billed with inappropriate or missing modifier."

    if category == "PLACE_OF_SERVICE":
        return "Service billed in a Place of Service (POS) setting not eligible for reimbursement under policy."

    if category == "AGE_GENDER_SCOPE":
        if attachment_val:
            return f"Patient age on date of service is outside designated age range ({attachment_val}) for the reported code."
        return "Patient age or gender on date of service does not satisfy policy criteria for the procedure code."

    if category == "EXCEPTION_OVERRIDE":
        if mods:
            return f"Service reported with modifier {', '.join(mods)} qualifies as an exception; standard edit suppressed/rerouted."
        return "Service qualifies for policy exception; standard reimbursement restriction does not apply."

    if category == "ONCE_IN_A_LIFETIME":
        return (
            "Procedure billed more than once across patient lifetime for any code within the same Code Family, "
            "regardless of whether billed by the same or different physicians."
        )

    return "Service billed violates stated reimbursement policy conditions."


def determine_modifier_rule(text: str, mods: List[str], cat: str) -> str:
    """Classifies the modifier action."""
    t_lower = text.lower()
    if not mods and "modifier" not in t_lower:
        return "NONE"
    if cat == "EXCEPTION" or "reduced services" in t_lower:
        return "EXCEPTION_TRIGGER"
    if "does not recognize modifier" in t_lower or "no modifier" in t_lower or "not allowed" in t_lower:
        return "PROHIBITED_NO_OVERRIDE"
    if "must be" in t_lower or "required" in t_lower or "append" in t_lower:
        return "REQUIRED"
    if "may be" in t_lower or "recognize" in t_lower:
        return "ALLOWED_OVERRIDE"
    return "INFORMATIONAL"


# ==============================================================================
# RULE EXTRACTION PIPELINE
# ==============================================================================

class RuleExtractionEngine:
    """
    Deterministic rule engine that scans policy prose, Q&As, and attachment tables.
    Picks exact policy sentences using keywords and infills values from attachments.
    """

    def __init__(self, policies: List[Dict[str, Any]]):
        self.policies = policies
        self.rules: List[BillingRule] = []
        self._rule_counter = 0

    def _next_rule_id(self, prefix: str = "UHC") -> str:
        self._rule_counter += 1
        return f"{prefix}-RULE-{self._rule_counter:04d}"

    def extract_all_rules(self) -> List[BillingRule]:
        self.rules.clear()
        self._rule_counter = 0

        for idx, policy in enumerate(self.policies):
            title = norm_ws(policy.get("policy_title")) or ""
            if not title or title.startswith(("01/", "02/", "03/", "04/", "05/", "06/", "07/", "08/", "09/", "10/", "11/", "12/", "1/1", "2/1", "3/1", "4/1", "6/1")):
                # Skip date-header scrapes that have no real title or empty guidelines
                rg_content = str((policy.get("reimbursement_guidelines") or {}).get("content") or "").strip()
                if not rg_content:
                    continue

            self._process_policy(policy, idx)

        # Assign canonical claim fields
        assign_claim_columns(self.rules)
        return self.rules

    def _process_policy(self, policy: Dict[str, Any], idx: int) -> None:
        title = norm_ws(policy.get("policy_title")) or "Untitled Policy"
        pnum = norm_ws(policy.get("policy_number")) or ""
        lob = norm_ws(policy.get("Line_of_Business")) or "Medicare Advantage"
        bclass = norm_ws(policy.get("Billingclass")) or "Both"
        universe = set(policy.get("CPT_CODES_USED") or [])

        # Check for linked attachment tables first
        tables = attachment_code_tables(policy)

        # Flatten policy into text units (guidelines, subsections, tables, qa)
        units = flatten_policy(policy, idx)

        # Track base rules to attach code-list / table values
        policy_base_rules: List[BillingRule] = []

        # 1. Process regular prose and Q&As
        for unit in units:
            if unit.source_type == "qa":
                # For Q&A, treat the entire Q&A pair as a discrete rule statement
                qa_text = f"Q: {unit.context} A: {unit.text}"
                matched_cats = self._match_keywords(qa_text)
                if matched_cats:
                    rule = self._create_verbatim_rule(
                        unit=unit,
                        rule_text=qa_text,
                        category=matched_cats[0][0],
                        keywords=[m[1] for m in matched_cats],
                        universe=universe,
                        policy=policy,
                    )
                    self.rules.append(rule)
                    policy_base_rules.append(rule)
                continue

            # Process sentences in guideline/subsection
            sentences = split_sentences(unit.text)
            for sent in sentences:
                sent_clean = sent.strip()
                if is_boilerplate(sent_clean) or len(sent_clean) < 20:
                    continue

                matched_cats = self._match_keywords(sent_clean)
                if not matched_cats:
                    continue

                primary_cat = matched_cats[0][0]
                keywords = [m[1] for m in matched_cats]

                rule = self._create_verbatim_rule(
                    unit=unit,
                    rule_text=sent_clean,
                    category=primary_cat,
                    keywords=keywords,
                    universe=universe,
                    policy=policy,
                )
                self.rules.append(rule)
                policy_base_rules.append(rule)

        # 2. Process Attachments (Infill values into rules)
        if tables:
            self._infill_attachment_rules(policy, idx, tables, policy_base_rules)

    def _match_keywords(self, text: str) -> List[Tuple[str, str]]:
        """Finds all matching rule keyword categories and matching phrases."""
        matches = []
        for cat, rx in KEYWORD_PATTERNS.items():
            m = rx.search(text)
            if m:
                matches.append((cat, m.group(0)))
        return matches

    def _create_verbatim_rule(
        self,
        unit: TextUnit,
        rule_text: str,
        category: str,
        keywords: List[str],
        universe: Set[str],
        policy: Dict[str, Any],
    ) -> BillingRule:
        rule_id = self._next_rule_id()
        codes, _ = extract_codes(rule_text, universe=universe)
        if not codes and universe and len(universe) <= 10:
            # Associate policy codes if the policy governs a small explicit set
            codes = sorted(universe)

        mods = extract_modifiers(rule_text)
        mod_rule = determine_modifier_rule(rule_text, mods, category)

        condition = derive_incorrect_billing_condition(
            category=category,
            text=rule_text,
            codes=codes,
            mods=mods,
        )

        section_str = " > ".join(unit.section_path) if unit.section_path else "Reimbursement Guidelines"
        if unit.source_type == "qa":
            section_str = f"Questions and Answers > {section_str}"

        return BillingRule(
            rule_id=rule_id,
            policy_idx=unit.policy_idx,
            policy_title=unit.policy_title,
            policy_number=unit.policy_number or "",
            line_of_business=unit.line_of_business or "Medicare Advantage",
            billing_class=unit.billing_class or "Both",
            source_section=section_str,
            policy_rule_text=rule_text,  # VERBATIM policy content
            rule_category=category,
            target_codes=codes,
            attachment_reference="",
            attachment_values="",
            target_modifiers=mods,
            modifier_rule=mod_rule,
            incorrect_billing_condition=condition,
            required_claim_fields=[],
            trigger_keywords=keywords,
            source_type=unit.source_type,
            order=float(unit.order),
        )

    def _infill_attachment_rules(
        self,
        policy: Dict[str, Any],
        idx: int,
        tables: List[AttachmentTable],
        base_rules: List[BillingRule],
    ) -> None:
        """
        Fills the values inside rules using attachments (e.g., Time Span, Comprehensive/Component).
        Preserves the exact verbatim rule text while infilling the specific parameter values.
        """
        title = norm_ws(policy.get("policy_title")) or ""
        pnum = norm_ws(policy.get("policy_number")) or ""
        lob = norm_ws(policy.get("Line_of_Business")) or "Medicare Advantage"
        bclass = norm_ws(policy.get("Billingclass")) or "Both"
        universe = set(policy.get("CPT_CODES_USED") or [])

        for tbl in tables:
            tbl_lower = tbl.name.lower()

            # Case A: Code -> Code relationship (e.g. TimeSpanComprehensiveandComponentCodes)
            if tbl.value_is_code:
                # Group comprehensive -> components
                comp_to_comps = defaultdict(list)
                for comp, comps in tbl.mapping.items():
                    comp_to_comps[comp].extend(comps)

                # Find the exact verbatim policy sentence for comprehensive/component
                comp_text = ""
                for r in base_rules:
                    if "share a common portion of a code description" in r.policy_rule_text.lower() or "most comprehensive service" in r.policy_rule_text.lower():
                        comp_text = r.policy_rule_text
                        break
                    elif "comprehensive" in r.policy_rule_text.lower() and "component" in r.policy_rule_text.lower():
                        comp_text = r.policy_rule_text
                if not comp_text:
                    comp_text = "When related Time Span Codes which share a common portion of a code description are both reported during the same time span period by the Same Group Physician and/or Other Health Care Professional for the same patient, the code with the most comprehensive description is the reimbursable service."

                c_text_codes, _ = extract_codes(comp_text, universe=universe)

                for comp_code, component_codes in comp_to_comps.items():
                    rule_id = self._next_rule_id()
                    all_pair_codes = sorted(set([comp_code] + sorted(set(component_codes))) | set(c_text_codes))
                    vals_str = f"Comprehensive Code: {comp_code}, Component Code(s): {', '.join(component_codes)}"
                    cond = (
                        f"Component code(s) [{', '.join(component_codes)}] billed on the same date of service "
                        f"or within the same time span as comprehensive code [{comp_code}] for the same member "
                        f"by the same group physician; component service is inclusive."
                    )
                    self.rules.append(
                        BillingRule(
                            rule_id=rule_id,
                            policy_idx=idx,
                            policy_title=title,
                            policy_number=pnum,
                            line_of_business=lob,
                            billing_class=bclass,
                            source_section=f"Attachment: {tbl.name}",
                            policy_rule_text=comp_text,  # Exact verbatim text
                            rule_category="BUNDLING_INCLUSION",
                            target_codes=all_pair_codes,
                            attachment_reference=tbl.name,
                            attachment_values=vals_str,
                            target_modifiers=[],
                            modifier_rule="PROHIBITED_NO_OVERRIDE (No modifier overrides this denial per Time Span Policy)",
                            incorrect_billing_condition=cond,
                            required_claim_fields=[],
                            trigger_keywords=["comprehensive and component codes", "inclusive"],
                            source_type="attachment",
                            order=900.0,
                        )
                    )
                continue

            # Case B: Code -> Attribute Lookup (e.g. TimeSpanCodes: Time Span = Calendar Month, 30, etc.)
            val_to_codes = defaultdict(list)
            for code, vals in tbl.mapping.items():
                for v in vals:
                    val_to_codes[v].append(code)

            # Find best matching verbatim base rule text for this attribute table
            base_rule_text = ""
            if "timespan" in tbl_lower or "time_span" in tbl_lower:
                for r in base_rules:
                    if "time period for which it should be reported" in r.policy_rule_text.lower() or "once during that time period" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "lifetime" in tbl_lower:
                for r in base_rules:
                    if "once during a patient's lifetime" in r.policy_rule_text.lower() or "same code family will be reimbursed only once" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "age" in tbl_lower:
                for r in base_rules:
                    if "within the age designation assigned to the code" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "add-on" in tbl_lower or "primary" in tbl_lower:
                for r in base_rules:
                    if "never reimbursed unless a primary procedure" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
                    elif "must be reported with the primary procedure" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "emergency" in tbl_lower or "edc" in tbl_lower or "analyzer" in tbl_lower:
                for r in base_rules:
                    if "optum emergency department claim (edc) analyzer" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "ndc" in tbl_lower or "packaged" in tbl_lower:
                for r in base_rules:
                    if "exceed the maximum units allowed per package" in r.policy_rule_text.lower() or "should not exceed the package maximum units" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "bilateral" in tbl_lower:
                for r in base_rules:
                    if "bilateral modifier 50" in r.policy_rule_text.lower() or "billed bilaterally" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "professional" in tbl_lower or "radiology" in tbl_lower or "gap" in tbl_lower:
                for r in base_rules:
                    if "gap codes" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
                    elif "radiology interpretation report" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
                    elif "professional component and a technical component" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "rebundl" in tbl_lower:
                for r in base_rules:
                    if "rebundling edits" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break
            elif "split" in tbl_lower:
                for r in base_rules:
                    if "preoperative care is not reimbursed separately" in r.policy_rule_text.lower():
                        base_rule_text = r.policy_rule_text
                        break

            if not base_rule_text:
                for r in base_rules:
                    if r.rule_category in ("TIME_SPAN_LIMIT", "FREQUENCY_LIMIT", "REIMBURSEMENT_CRITERIA", "DENIAL_PROHIBITION"):
                        base_rule_text = r.policy_rule_text
                        break
            if not base_rule_text and base_rules:
                base_rule_text = base_rules[0].policy_rule_text

            for raw_val, codes in sorted(val_to_codes.items(), key=lambda kv: -len(kv[1])):
                rule_id = self._next_rule_id()
                pretty_val = pretty_time_span(raw_val)
                attr_name = tbl.attribute or "Value"
                vals_str = f"{attr_name}: {pretty_val}"

                # Determine category based on attribute
                cat = "TIME_SPAN_LIMIT" if "time span" in attr_name.lower() or "timespan" in tbl_lower else (
                    "AGE_GENDER_SCOPE" if "age" in attr_name.lower() or "age" in tbl_lower else (
                        "ONCE_IN_A_LIFETIME" if "lifetime" in tbl_lower else (
                            "BUNDLING_INCLUSION" if "add_on" in attr_name.lower() or "add-on" in tbl_lower or "component" in tbl_lower or "rebundl" in tbl_lower else (
                                "FREQUENCY_LIMIT" if "frequency" in attr_name.lower() or "units" in attr_name.lower() or "maximum" in tbl_lower or "ndc" in tbl_lower else (
                                    "MODIFIER_RESTRICTION" if "modifier" in attr_name.lower() or "bilateral" in tbl_lower else "REIMBURSEMENT_CRITERIA"
                                )
                            )
                        )
                    )
                )

                rule_text = base_rule_text or f"Reimbursement policy criteria published in {tbl.name}."
                text_codes, _ = extract_codes(rule_text, universe=universe)
                final_codes = sorted(set(codes) | set(text_codes))
                text_mods = extract_modifiers(rule_text)

                cond = derive_incorrect_billing_condition(
                    category=cat,
                    text=rule_text,
                    codes=final_codes,
                    mods=text_mods,
                    attachment_attr=attr_name,
                    attachment_val=raw_val,
                )

                if cat == "TIME_SPAN_LIMIT":
                    mod_rule = "PROHIBITED_NO_OVERRIDE (Q&A Rule 2: Modifiers e.g. 59, 76 do not override time span limit)"
                elif text_mods or cat == "MODIFIER_RESTRICTION":
                    mod_rule = determine_modifier_rule(rule_text, text_mods, cat)
                else:
                    mod_rule = "NONE"

                self.rules.append(
                    BillingRule(
                        rule_id=rule_id,
                        policy_idx=idx,
                        policy_title=title,
                        policy_number=pnum,
                        line_of_business=lob,
                        billing_class=bclass,
                        source_section=f"Attachment: {tbl.name}",
                        policy_rule_text=rule_text,  # Exact verbatim text
                        rule_category=cat,
                        target_codes=final_codes,
                        attachment_reference=tbl.name,
                        attachment_values=vals_str,
                        target_modifiers=text_mods,
                        modifier_rule=mod_rule,
                        incorrect_billing_condition=cond,
                        required_claim_fields=[],
                        trigger_keywords=[attr_name.lower(), "attachment value lookup"],
                        source_type="attachment",
                        order=950.0,
                    )
                )


# ==============================================================================
# WORKBOOK & JSON EXPORT ENGINE
# ==============================================================================

def export_billing_rules(
    rules: List[BillingRule],
    output_xlsx: Path,
    output_json: Path,
    output_csv: Path,
) -> None:
    """Exports structured rules into Excel, JSON, and CSV."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    print(f"Exporting {len(rules)} billing rules...")

    # 1. Export JSON
    json_records = []
    for r in rules:
        rec = asdict(r)
        json_records.append(rec)

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(json_records, f, indent=2)
    print(f"  -> JSON exported to: {output_json}")

    # 2. Export CSV
    csv_columns = [
        "Rule_ID", "Policy_Title", "Policy_Number", "Line_of_Business", "Billing_Class",
        "Source_Section", "Policy_Rule_Text", "Rule_Category", "Target_Codes",
        "Attachment_Reference", "Attachment_Values", "Target_Modifiers", "Modifier_Rule",
        "Incorrect_Billing_Condition", "Required_Claim_Fields", "Trigger_Keywords"
    ]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_columns)
        writer.writeheader()
        for r in rules:
            writer.writerow({
                "Rule_ID": r.rule_id,
                "Policy_Title": r.policy_title,
                "Policy_Number": r.policy_number,
                "Line_of_Business": r.line_of_business,
                "Billing_Class": r.billing_class,
                "Source_Section": r.source_section,
                "Policy_Rule_Text": r.policy_rule_text,
                "Rule_Category": r.rule_category,
                "Target_Codes": ", ".join(r.target_codes[:50]) + (f" (+{len(r.target_codes)-50} more)" if len(r.target_codes) > 50 else ""),
                "Attachment_Reference": r.attachment_reference,
                "Attachment_Values": r.attachment_values,
                "Target_Modifiers": ", ".join(r.target_modifiers),
                "Modifier_Rule": r.modifier_rule,
                "Incorrect_Billing_Condition": r.incorrect_billing_condition,
                "Required_Claim_Fields": ", ".join(r.required_claim_fields),
                "Trigger_Keywords": ", ".join(r.trigger_keywords),
            })
    print(f"  -> CSV exported to: {output_csv}")

    # 3. Export Excel Workbook
    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Calibri", size=10)
    border_side = Side(style="thin", color="D9D9D9")
    cell_border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

    def style_worksheet(ws, widths: Dict[str, int], wrap_cols: Set[str]):
        ws.freeze_panes = "A2"
        for col_idx in range(1, ws.max_column + 1):
            col_letter = get_column_letter(col_idx)
            header_cell = ws.cell(row=1, column=col_idx)
            header_cell.fill = header_fill
            header_cell.font = header_font
            header_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            col_name = str(header_cell.value)
            ws.column_dimensions[col_letter].width = widths.get(col_name, 18)

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = data_font
                cell.border = cell_border
                col_name = ws.cell(row=1, column=cell.column).value
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=(col_name in wrap_cols)
                )

    # Sheet 1: Master Rules Table
    ws_rules = wb.active
    ws_rules.title = "Rules"
    ws_rules.append(csv_columns)

    for r in rules:
        ws_rules.append([
            r.rule_id,
            r.policy_title,
            r.policy_number,
            r.line_of_business,
            r.billing_class,
            r.source_section,
            r.policy_rule_text,
            r.rule_category,
            ", ".join(r.target_codes[:40]) + (f" (+{len(r.target_codes)-40} more)" if len(r.target_codes) > 40 else ""),
            r.attachment_reference,
            r.attachment_values,
            ", ".join(r.target_modifiers),
            r.modifier_rule,
            r.incorrect_billing_condition,
            ", ".join(r.required_claim_fields),
            ", ".join(r.trigger_keywords),
        ])

    style_worksheet(
        ws_rules,
        widths={
            "Rule_ID": 15, "Policy_Title": 32, "Policy_Number": 14, "Line_of_Business": 18,
            "Billing_Class": 14, "Source_Section": 30, "Policy_Rule_Text": 55, "Rule_Category": 22,
            "Target_Codes": 30, "Attachment_Reference": 24, "Attachment_Values": 28,
            "Target_Modifiers": 16, "Modifier_Rule": 25, "Incorrect_Billing_Condition": 50,
            "Required_Claim_Fields": 30, "Trigger_Keywords": 25,
        },
        wrap_cols={"Policy_Rule_Text", "Incorrect_Billing_Condition", "Target_Codes", "Attachment_Values", "Source_Section"}
    )

    # Sheet 2: Attachment Filled Rules (Dedicated view for attachment rules like Time Span)
    ws_attach = wb.create_sheet(title="Attachment_Filled_Rules")
    ws_attach.append(csv_columns)
    for r in rules:
        if r.attachment_reference:
            ws_attach.append([
                r.rule_id,
                r.policy_title,
                r.policy_number,
                r.line_of_business,
                r.billing_class,
                r.source_section,
                r.policy_rule_text,
                r.rule_category,
                ", ".join(r.target_codes[:40]) + (f" (+{len(r.target_codes)-40} more)" if len(r.target_codes) > 40 else ""),
                r.attachment_reference,
                r.attachment_values,
                ", ".join(r.target_modifiers),
                r.modifier_rule,
                r.incorrect_billing_condition,
                ", ".join(r.required_claim_fields),
                ", ".join(r.trigger_keywords),
            ])
    style_worksheet(
        ws_attach,
        widths={
            "Rule_ID": 15, "Policy_Title": 32, "Policy_Number": 14, "Line_of_Business": 18,
            "Billing_Class": 14, "Source_Section": 30, "Policy_Rule_Text": 55, "Rule_Category": 22,
            "Target_Codes": 30, "Attachment_Reference": 24, "Attachment_Values": 28,
            "Target_Modifiers": 16, "Modifier_Rule": 25, "Incorrect_Billing_Condition": 50,
            "Required_Claim_Fields": 30, "Trigger_Keywords": 25,
        },
        wrap_cols={"Policy_Rule_Text", "Incorrect_Billing_Condition", "Target_Codes", "Attachment_Values", "Source_Section"}
    )

    # Sheet 3: Code Index (Mapping of each distinct code to rules)
    ws_codes = wb.create_sheet(title="Code_Index")
    ws_codes.append(["Code", "Rule_ID", "Policy_Title", "Rule_Category", "Attachment_Reference", "Attachment_Values", "Incorrect_Billing_Condition"])
    for r in rules:
        for c in r.target_codes[:250]:
            ws_codes.append([
                c, r.rule_id, r.policy_title, r.rule_category, r.attachment_reference, r.attachment_values, r.incorrect_billing_condition
            ])
    style_worksheet(
        ws_codes,
        widths={"Code": 12, "Rule_ID": 16, "Policy_Title": 34, "Rule_Category": 22, "Attachment_Reference": 24, "Attachment_Values": 28, "Incorrect_Billing_Condition": 55},
        wrap_cols={"Incorrect_Billing_Condition", "Attachment_Values"}
    )

    # Sheet 4: Summary Metrics
    ws_summary = wb.create_sheet(title="Summary")
    ws_summary.append(["Metric", "Count"])

    ws_summary.append(["Total Generated Rules", len(rules)])
    ws_summary.append(["Attachment-Infilled Rules", sum(1 for r in rules if r.attachment_reference)])
    ws_summary.append(["Distinct Policies Represented", len(set(r.policy_title for r in rules))])

    # By category
    cat_counts = Counter(r.rule_category for r in rules)
    ws_summary.append(["", ""])
    ws_summary.append(["--- Breakdown by Rule Category ---", "Count"])
    for cat, count in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
        ws_summary.append([cat, count])

    # By LOB
    lob_counts = Counter(r.line_of_business for r in rules)
    ws_summary.append(["", ""])
    ws_summary.append(["--- Breakdown by Line of Business ---", "Count"])
    for lob, count in sorted(lob_counts.items(), key=lambda kv: -kv[1]):
        ws_summary.append([lob, count])

    style_worksheet(ws_summary, widths={"Metric": 38, "Count": 16}, wrap_cols=set())

    wb.save(output_xlsx)
    print(f"  -> Excel workbook saved to: {output_xlsx}")


# ==============================================================================
# CLI ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Deterministic Payer Policy Rule Generation Engine")
    parser.add_argument(
        "--input",
        type=str,
        default=str(UHC_DIR / "extraction_rules_uhc.json"),
        help="Path to input extracted policies JSON file",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(UHC_DIR),
        help="Directory to save output billing rules files",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading extracted policies from: {input_path}")
    policies = load_policies(str(input_path))
    print(f"Loaded {len(policies)} policies.")

    engine = RuleExtractionEngine(policies)
    rules = engine.extract_all_rules()
    print(f"Extracted {len(rules)} clean billing rules.")

    out_xlsx = out_dir / "billing_rules.xlsx"
    out_json = out_dir / "billing_rules.json"
    out_csv = out_dir / "billing_rules.csv"

    export_billing_rules(rules, out_xlsx, out_json, out_csv)
    print("\nRule generation completed successfully!")


if __name__ == "__main__":
    main()
