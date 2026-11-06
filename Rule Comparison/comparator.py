"""
comparator.py — Classify each matched rule pair into a comparison finding.

For every pair produced by matcher.py, we examine the substance of the
two rules along five dimensions and emit a structured finding:

1. EQUIVALENT     — same clinical intent, same constraints
2. DIFFERING      — same topic, materially different requirements / limits
3. CONFLICTING    — directly contradictory (one allows, other denies)
4. ONE_SIDED      — topic covered by only one payer
5. NOT_ASSESSABLE — text too generic to compare meaningfully

Each finding also carries a "gap_detail" dict with specifics:
  - modifier_conflict: modifiers required by A vs prohibited by B
  - frequency_conflict: different limit values detected
  - code_gap: codes in A not in B (and vice-versa)
  - category_mismatch: canonical categories differ
  - statement_delta: how much the verbatim text diverges
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz

from matcher import RuleMatch


# ---------------------------------------------------------------------------
# Keyword banks for detecting specific rule attributes
# ---------------------------------------------------------------------------

_DENY_TOKENS = {"not eligible", "will not be reimbursed", "not covered",
                "denied", "non-covered", "noncovered", "not reimbursed",
                "not payable", "excluded"}

_ALLOW_TOKENS = {"eligible for reimbursement", "will be reimbursed",
                 "covered", "payable", "reimbursed when", "consider reimbursement"}

_FREQ_PATTERN  = re.compile(
    r"(?P<n>\d+)\s*(?:time|visit|unit|occurrence|per year|per day|per month"
    r"|per 365|per 90|per 30|lifetime)",
    re.IGNORECASE,
)
_DAYS_PATTERN  = re.compile(r"(\d+)\s*day", re.IGNORECASE)
_UNITS_PATTERN = re.compile(r"maximum\s*(?:of\s*)?(\d+)\s*unit", re.IGNORECASE)


def _has_deny(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in _DENY_TOKENS)


def _has_allow(text: str) -> bool:
    t = text.lower()
    return any(tok in t for tok in _ALLOW_TOKENS)


def _extract_freq_values(text: str) -> list:
    vals = []
    for m in _FREQ_PATTERN.finditer(text):
        vals.append(m.group(0).lower().strip())
    for m in _DAYS_PATTERN.finditer(text):
        vals.append(f"{m.group(1)} days")
    for m in _UNITS_PATTERN.finditer(text):
        vals.append(f"max {m.group(1)} units")
    return list(set(vals))


def _text_equiv(ta: str, tb: str, threshold: float = 0.80) -> bool:
    return fuzz.token_set_ratio(ta, tb) / 100.0 >= threshold


# ---------------------------------------------------------------------------
# Finding dataclass
# ---------------------------------------------------------------------------

@dataclass
class ComparisonFinding:
    payer_a:        str
    rule_id_a:      str
    policy_title_a: str
    rule_category_a:str
    statement_a:    str
    codes_a:        str         # comma-sep string

    payer_b:        str
    rule_id_b:      str
    policy_title_b: str
    rule_category_b:str
    statement_b:    str
    codes_b:        str

    shared_codes:   str
    composite_score:float
    match_tier:     str

    finding_type:   str         # EQUIVALENT | DIFFERING | CONFLICTING | ONE_SIDED | NOT_ASSESSABLE
    finding_detail: str         # human-readable explanation

    # Structured gap data
    codes_only_in_a:        str = ""
    codes_only_in_b:        str = ""
    freq_values_a:          str = ""
    freq_values_b:          str = ""
    modifier_conflict:      str = ""
    category_mismatch:      bool = False
    statement_similarity:   float = 0.0


# ---------------------------------------------------------------------------
# Core classifier
# ---------------------------------------------------------------------------

def classify(match: RuleMatch, row_a: pd.Series, row_b: pd.Series) -> ComparisonFinding:
    """
    Classify a single matched pair into a comparison finding.

    Parameters
    ----------
    match : RuleMatch produced by matcher.match_rules()
    row_a : full canonical row for rule A (from ingest.load_all())
    row_b : full canonical row for rule B
    """
    sa = str(match.statement_a)
    sb = str(match.statement_b)
    stmt_sim = fuzz.token_set_ratio(sa, sb) / 100.0

    codes_a = set(match.codes_a)
    codes_b = set(match.codes_b)
    only_a  = sorted(codes_a - codes_b)
    only_b  = sorted(codes_b - codes_a)

    freq_a = _extract_freq_values(sa)
    freq_b = _extract_freq_values(sb)

    mods_req_a   = set(row_a["modifiers_required"]   if isinstance(row_a["modifiers_required"], list)   else [])
    mods_req_b   = set(row_b["modifiers_required"]   if isinstance(row_b["modifiers_required"], list)   else [])
    mods_proh_a  = set(row_a["modifiers_prohibited"] if isinstance(row_a["modifiers_prohibited"], list) else [])
    mods_proh_b  = set(row_b["modifiers_prohibited"] if isinstance(row_b["modifiers_prohibited"], list) else [])

    # Modifier conflict: A requires what B prohibits (or vice versa)
    mod_conflict_parts = []
    for m in mods_req_a & mods_proh_b:
        mod_conflict_parts.append(f"{match.payer_a} requires {m}; {match.payer_b} prohibits {m}")
    for m in mods_req_b & mods_proh_a:
        mod_conflict_parts.append(f"{match.payer_b} requires {m}; {match.payer_a} prohibits {m}")
    mod_conflict_str = " | ".join(mod_conflict_parts)

    cat_mismatch = match.rule_category_a != match.rule_category_b

    # --- Determine finding type ---
    deny_a  = _has_deny(sa)
    deny_b  = _has_deny(sb)
    allow_a = _has_allow(sa)
    allow_b = _has_allow(sb)

    # CONFLICTING: one explicitly denies, the other explicitly allows
    if (deny_a and allow_b) or (deny_b and allow_a):
        ftype = "CONFLICTING"
        fdetail = (
            f"{match.payer_a} {'denies' if deny_a else 'allows'} and "
            f"{match.payer_b} {'denies' if deny_b else 'allows'} the same clinical scenario."
        )

    # CONFLICTING: direct modifier conflict
    elif mod_conflict_parts:
        ftype = "CONFLICTING"
        fdetail = f"Modifier conflict: {mod_conflict_str}"

    # EQUIVALENT: high text similarity and minimal code gap
    elif stmt_sim >= 0.75 and len(only_a) + len(only_b) <= 3:
        ftype = "EQUIVALENT"
        fdetail = (
            f"Rules are functionally equivalent (statement similarity {stmt_sim:.0%}). "
            + (f"Minor code set difference: only_in_{match.payer_a}=[{', '.join(only_a[:5])}] only_in_{match.payer_b}=[{', '.join(only_b[:5])}]"
               if only_a or only_b else "Code sets are identical.")
        )

    # DIFFERING: same topic, different frequency / time-span values
    elif freq_a and freq_b and set(freq_a) != set(freq_b):
        ftype = "DIFFERING"
        fdetail = (
            f"Frequency/limit values differ — "
            f"{match.payer_a}: {', '.join(freq_a)} | "
            f"{match.payer_b}: {', '.join(freq_b)}"
        )

    # DIFFERING: substantial code-set divergence
    elif (only_a or only_b) and (len(only_a) + len(only_b)) > 3:
        ftype = "DIFFERING"
        fdetail = (
            f"Code sets diverge — "
            f"{len(only_a)} codes only in {match.payer_a}, "
            f"{len(only_b)} codes only in {match.payer_b}. "
            f"Statement similarity {stmt_sim:.0%}."
        )

    # DIFFERING: category mismatch on a matched pair
    elif cat_mismatch:
        ftype = "DIFFERING"
        fdetail = (
            f"Same policy topic but different rule types: "
            f"{match.payer_a}={match.rule_category_a}, {match.payer_b}={match.rule_category_b}."
        )

    # DIFFERING: moderate text divergence
    elif 0.40 <= stmt_sim < 0.75:
        ftype = "DIFFERING"
        fdetail = (
            f"Similar topic but rules differ in substance (text similarity {stmt_sim:.0%}). "
            f"May represent differing clinical requirements."
        )

    # NOT_ASSESSABLE: text too short or too generic
    elif len(sa.split()) < 8 or len(sb.split()) < 8:
        ftype = "NOT_ASSESSABLE"
        fdetail = "Rule text too brief to assess substance."

    else:
        ftype = "DIFFERING"
        fdetail = f"Rules matched on shared codes/title but text diverges (similarity {stmt_sim:.0%})."

    return ComparisonFinding(
        payer_a=match.payer_a,
        rule_id_a=match.rule_id_a,
        policy_title_a=match.policy_title_a,
        rule_category_a=match.rule_category_a,
        statement_a=sa,
        codes_a=", ".join(match.codes_a[:20]),

        payer_b=match.payer_b,
        rule_id_b=match.rule_id_b,
        policy_title_b=match.policy_title_b,
        rule_category_b=match.rule_category_b,
        statement_b=sb,
        codes_b=", ".join(match.codes_b[:20]),

        shared_codes=", ".join(match.shared_codes),
        composite_score=match.composite,
        match_tier=match.match_tier,

        finding_type=ftype,
        finding_detail=fdetail,

        codes_only_in_a=", ".join(only_a[:30]),
        codes_only_in_b=", ".join(only_b[:30]),
        freq_values_a=", ".join(freq_a),
        freq_values_b=", ".join(freq_b),
        modifier_conflict=mod_conflict_str,
        category_mismatch=cat_mismatch,
        statement_similarity=round(stmt_sim, 4),
    )


def compare_all(
    matches: list,
    df_full: pd.DataFrame,
) -> List[ComparisonFinding]:
    """
    Classify every match.  Looks up full rows from df_full by rule_id + payer.
    """
    idx = df_full.set_index(["payer", "rule_id"]).sort_index()
    findings = []
    for m in matches:
        try:
            row_a = idx.loc[(m.payer_a, m.rule_id_a)]
            row_b = idx.loc[(m.payer_b, m.rule_id_b)]
        except KeyError:
            continue
        findings.append(classify(m, row_a, row_b))
    return findings


def findings_to_df(findings: List[ComparisonFinding]) -> pd.DataFrame:
    if not findings:
        return pd.DataFrame()
    return pd.DataFrame([f.__dict__ for f in findings])

