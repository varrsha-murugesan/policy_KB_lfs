"""
matcher.py — Find candidate equivalent rules across payers.

Matching strategy (four independent signals, combined into a composite score):

1. CPT/HCPCS code overlap  (Jaccard similarity on code sets)
2. Policy-title fuzzy match (rapidfuzz token_sort_ratio)
3. Rule-statement fuzzy match (rapidfuzz token_set_ratio, capped)
4. Canonical category match  (exact match on normalised category)

A pair is a *candidate match* when composite_score >= MATCH_THRESHOLD (default 0.40).
Pairs are further classified into:
  - STRONG  : composite >= 0.70
  - POSSIBLE: composite >= 0.50
  - WEAK    : composite >= 0.40

Design rationale
----------------
- Pairwise matching across all 3 payers (UHC×Aetna, UHC×Humana, Aetna×Humana).
- We only pair rules from DIFFERENT payers (no intra-payer comparison).
- Matching is O(n*m) but we pre-filter by canonical category to keep it fast
  for datasets of this size (~1000 rules each).
"""

from __future__ import annotations
import itertools
from dataclasses import dataclass, field
from typing import List, Tuple

import pandas as pd
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MATCH_THRESHOLD = 0.40     # minimum composite score to record a pair
STRONG_THRESHOLD = 0.70
POSSIBLE_THRESHOLD = 0.50

# Weights for each signal (must sum to 1.0)
W_CODE   = 0.40   # code overlap is the strongest anchor
W_TITLE  = 0.25   # policy topic match
W_TEXT   = 0.20   # rule-statement similarity
W_CAT    = 0.15   # canonical category


@dataclass
class RuleMatch:
    payer_a:        str
    rule_id_a:      str
    policy_title_a: str
    rule_category_a:str
    statement_a:    str
    codes_a:        list

    payer_b:        str
    rule_id_b:      str
    policy_title_b: str
    rule_category_b:str
    statement_b:    str
    codes_b:        list

    score_code:     float
    score_title:    float
    score_text:     float
    score_category: float
    composite:      float
    match_tier:     str           # STRONG | POSSIBLE | WEAK
    shared_codes:   list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _title_sim(ta: str, tb: str) -> float:
    """rapidfuzz token_sort_ratio normalised to [0, 1]."""
    return fuzz.token_sort_ratio(ta.lower(), tb.lower()) / 100.0


def _text_sim(ta: str, tb: str) -> float:
    """rapidfuzz token_set_ratio normalised to [0, 1], capped at 0.90
    to avoid trivially-short strings getting high scores."""
    if not ta.strip() or not tb.strip():
        return 0.0
    return min(fuzz.token_set_ratio(ta, tb) / 100.0, 0.90)


def _category_match(ca: str, cb: str) -> float:
    return 1.0 if ca == cb else 0.0


def _composite(sc, st, sx, scat) -> float:
    return W_CODE * sc + W_TITLE * st + W_TEXT * sx + W_CAT * scat


def _tier(score: float) -> str:
    if score >= STRONG_THRESHOLD:
        return "STRONG"
    if score >= POSSIBLE_THRESHOLD:
        return "POSSIBLE"
    return "WEAK"


# ---------------------------------------------------------------------------
# Main matching function
# ---------------------------------------------------------------------------

def match_rules(df: pd.DataFrame, threshold: float = MATCH_THRESHOLD) -> List[RuleMatch]:
    """
    Compare rules across every pair of payers and return all candidate matches.

    Parameters
    ----------
    df : canonical DataFrame from ingest.load_all()
    threshold : minimum composite score to retain a pair

    Returns
    -------
    list of RuleMatch dataclasses sorted by composite score descending
    """
    payers = df["payer"].unique().tolist()
    pairs  = list(itertools.combinations(payers, 2))
    matches: List[RuleMatch] = []

    for payer_a, payer_b in pairs:
        da = df[df["payer"] == payer_a].reset_index(drop=True)
        db = df[df["payer"] == payer_b].reset_index(drop=True)

        # Group by canonical category for coarse pre-filter
        cats_a = da.groupby("rule_category")
        cats_b = db["rule_category"].unique().tolist()

        for cat, group_a in cats_a:
            # Only compare within same category OR UNKNOWN on either side
            if cat == "UNKNOWN" or cat not in cats_b:
                candidates_b = db
            else:
                candidates_b = db[db["rule_category"] == cat]

            for _, row_a in group_a.iterrows():
                codes_a   = row_a["cpt_codes"] or []
                title_a   = str(row_a["policy_title"])
                stmt_a    = str(row_a["rule_statement"])
                cat_a     = str(row_a["rule_category"])

                for _, row_b in candidates_b.iterrows():
                    codes_b   = row_b["cpt_codes"] or []
                    title_b   = str(row_b["policy_title"])
                    stmt_b    = str(row_b["rule_statement"])
                    cat_b     = str(row_b["rule_category"])

                    # Quick reject: if both have codes and zero overlap, skip
                    if codes_a and codes_b and not (set(codes_a) & set(codes_b)):
                        # Still allow title-only matches if title sim is very high
                        if _title_sim(title_a, title_b) < 0.70:
                            continue

                    sc   = _jaccard(codes_a, codes_b)
                    st   = _title_sim(title_a, title_b)
                    sx   = _text_sim(stmt_a, stmt_b)
                    scat = _category_match(cat_a, cat_b)
                    comp = _composite(sc, st, sx, scat)

                    if comp >= threshold:
                        matches.append(RuleMatch(
                            payer_a=payer_a,
                            rule_id_a=row_a["rule_id"],
                            policy_title_a=title_a,
                            rule_category_a=cat_a,
                            statement_a=stmt_a,
                            codes_a=codes_a,
                            payer_b=payer_b,
                            rule_id_b=row_b["rule_id"],
                            policy_title_b=title_b,
                            rule_category_b=cat_b,
                            statement_b=stmt_b,
                            codes_b=codes_b,
                            score_code=round(sc, 4),
                            score_title=round(st, 4),
                            score_text=round(sx, 4),
                            score_category=round(scat, 4),
                            composite=round(comp, 4),
                            match_tier=_tier(comp),
                            shared_codes=sorted(set(codes_a) & set(codes_b)),
                        ))

    matches.sort(key=lambda m: m.composite, reverse=True)
    return matches


def matches_to_df(matches: List[RuleMatch]) -> pd.DataFrame:
    if not matches:
        return pd.DataFrame()
    rows = []
    for m in matches:
        rows.append({
            "payer_a":          m.payer_a,
            "rule_id_a":        m.rule_id_a,
            "policy_title_a":   m.policy_title_a,
            "rule_category_a":  m.rule_category_a,
            "statement_a":      m.statement_a,
            "codes_a":          ", ".join(m.codes_a[:20]),

            "payer_b":          m.payer_b,
            "rule_id_b":        m.rule_id_b,
            "policy_title_b":   m.policy_title_b,
            "rule_category_b":  m.rule_category_b,
            "statement_b":      m.statement_b,
            "codes_b":          ", ".join(m.codes_b[:20]),

            "shared_codes":     ", ".join(m.shared_codes),
            "shared_code_count":len(m.shared_codes),
            "score_code":       m.score_code,
            "score_title":      m.score_title,
            "score_text":       m.score_text,
            "score_category":   m.score_category,
            "composite_score":  m.composite,
            "match_tier":       m.match_tier,
        })
    return pd.DataFrame(rows)

