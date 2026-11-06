"""
gap_finder.py — Identify rules that exist in ONE payer but have NO equivalent
in the other payers (coverage gaps).

A rule is considered "unmatched" when matcher.py returns no pair for it above
the MATCH_THRESHOLD.  Unmatched rules represent:
  - Clinical scenarios covered only by that payer
  - Potential gaps in another payer's policy set
  - Risk areas where claim treatment may differ unpredictably

Output per gap entry
--------------------
payer          : payer that has the rule
rule_id        : identifier
policy_title   : policy name
rule_category  : canonical category
rule_statement : verbatim text
cpt_codes      : applicable codes
missing_in     : comma-separated payers that have NO equivalent rule
gap_severity   : HIGH | MEDIUM | LOW
  HIGH   = denial/frequency rule with shared codes — different treatment risk
  MEDIUM = reimbursement or modifier rule — could cause inconsistent billing
  LOW    = administrative / documentation rule — lower direct claim impact
"""

from __future__ import annotations
from typing import List, Set

import pandas as pd

from matcher import RuleMatch


_HIGH_RISK_CATS = {"DENIAL", "FREQUENCY_LIMIT"}
_MED_RISK_CATS  = {"REIMBURSEMENT_CRITERIA", "MODIFIER_RULE", "BUNDLING",
                   "PLACE_OF_SERVICE", "AGE_RESTRICTION"}


def _severity(rule_category: str, cpt_codes: list) -> str:
    if rule_category in _HIGH_RISK_CATS and len(cpt_codes) > 0:
        return "HIGH"
    if rule_category in _MED_RISK_CATS:
        return "MEDIUM"
    return "LOW"


def find_gaps(
    df_all: pd.DataFrame,
    matches: List[RuleMatch],
    threshold: float = 0.40,
) -> pd.DataFrame:
    """
    Parameters
    ----------
    df_all    : canonical DataFrame from ingest.load_all()
    matches   : match list from matcher.match_rules()
    threshold : composite score minimum to consider a rule "matched"

    Returns
    -------
    DataFrame of unmatched rules with gap severity and missing_in columns.
    """
    payers = df_all["payer"].unique().tolist()

    matched: dict = {p: set() for p in payers}
    for m in matches:
        if m.composite >= threshold:
            matched[m.payer_a].add(m.rule_id_a)
            matched[m.payer_b].add(m.rule_id_b)

    rows = []
    for _, rule in df_all.iterrows():
        p   = rule["payer"]
        rid = rule["rule_id"]

        if rid not in matched[p]:
            others = [x for x in payers if x != p]
            codes = rule["cpt_codes"] if isinstance(rule["cpt_codes"], list) else []
            rows.append({
                "payer":          p,
                "rule_id":        rid,
                "policy_title":   rule["policy_title"],
                "rule_category":  rule["rule_category"],
                "rule_statement": rule["rule_statement"],
                "cpt_codes":      ", ".join(codes[:30]),
                "code_count":     len(codes),
                "missing_in":     ", ".join(others),
                "gap_severity":   _severity(rule["rule_category"], codes),
            })

    if not rows:
        return pd.DataFrame()

    df_gaps = pd.DataFrame(rows)
    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    df_gaps["_sev_ord"] = df_gaps["gap_severity"].map(sev_order)
    df_gaps = (df_gaps
               .sort_values(["_sev_ord", "payer", "rule_category"])
               .drop(columns=["_sev_ord"])
               .reset_index(drop=True))
    return df_gaps
