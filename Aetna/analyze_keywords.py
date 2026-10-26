"""
analyze_keywords.py
===================
Mines rule indicators and signal keywords from Aetna policy corpus (extraction_rules.json).
Outputs rule_keywords.csv conforming to the reference schema in UHC billing_rules.xlsx.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

from policy_lib import flatten_policy, is_boilerplate, load_policies, norm_ws, split_sentences

# ----------------------------------------------------------------------------
# Weak Labelling Anchors
# ----------------------------------------------------------------------------

RULE_ANCHORS = [
    r"\bwill (?:only )?reimburse",
    r"\bwill be denied\b",
    r"\bwill be denied when\b",
    r"\bdenied as non-?reimbursable",
    r"\bnot separately (?:reported|reimbursed|payable)",
    r"\bshould (?:not )?be (?:reported|billed|submitted|appended|used)",
    r"\bmust be (?:reported|billed|submitted|appended|used|accompanied|present)",
    r"\bmay (?:not )?be (?:reported|billed|reimbursed|submitted|separately)",
    r"\bonly once (?:per|during|within)",
    r"\bonce per (?:day|month|year|calendar|patient|member|date|episode|lifetime)",
    r"\bno more than\b",
    r"\bmore than once\b",
    r"\bmaximum (?:unit|of|frequency)\b",
    r"\blimited to\b",
    r"\bconsidered (?:incidental|inclusive|included|part of|integral|mutually exclusive)",
    r"\brequires? (?:the use of )?modifier",
    r"\bappend(?:ed|ing)? (?:the )?modifier",
    r"\bhighest level of specificity\b",
    r"\bwithin (?:last )?(?:three years|\d+ days|eight-week|six-month|two-month)\b",
    r"\bappropriate reporting includes\b",
]

_RULE_RE = re.compile("|".join(RULE_ANCHORS), re.I)

NONRULE_ANCHORS = [
    r"\bproprietary\b",
    r"\ball rights reserved\b",
    r"\bdepartment: medical\b",
    r"\beffective date:\b",
    r"\breview/revision history\b",
    r"\bdefinitions:\b",
    r"\blegal/contract reference\b",
    r"\bstatement of objective:\b",
    r"\bpurpose:\b",
]

_NONRULE_RE = re.compile("|".join(NONRULE_ANCHORS), re.I)

# ----------------------------------------------------------------------------
# Categories & Scoring
# ----------------------------------------------------------------------------

CATEGORY_PATTERNS = [
    ("denial_prohibition", re.compile(
        r"deni\w*|not (?:separately )?(?:reimburs\w*|pay\w*|allow\w*)|will not|non-?reimburs\w*|"
        r"inappropriate|never event|unauthorized|prohibit\w*", re.I)),
    ("reimbursement_action", re.compile(
        r"will (?:only )?reimburse|reimburs\w*|pay\w*|allow\w*|cover\w*|payable|eligible", re.I)),
    ("frequency_limit", re.compile(
        r"once|per (?:day|date|year|month|week|lifetime)|maximum unit|no more than|more than once|"
        r"timeframe|period|frequency|eight-week|six-month|1,095-day|unit limit", re.I)),
    ("modifier_usage", re.compile(
        r"modifier|append|25|59|24|26|tc|jw|qw|anatomical|bilateral", re.I)),
    ("bundling_inclusion", re.compile(
        r"incidental|inclusive|integral|component|bundled|separately reported|not separately", re.I)),
    ("place_of_service", re.compile(
        r"place of service|pos\b|pos 11|pos 21|pos 24|pos 12|pos 53|office|facility setting|asc|home", re.I)),
    ("eligibility_condition", re.compile(
        r"highest level of specificity|supporting diagnosis|approved icd|medical necessity|age|pediatric|adult", re.I)),
    ("documentation", re.compile(
        r"medical record documentation|expected to support|support the specific", re.I)),
    ("coding_requirement", re.compile(
        r"reported using|submitted with|hcpcs level ii|cpt code|appropriate reporting", re.I)),
]

STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but",
    "by", "can", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just",
    "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "this", "those", "through", "to", "too",
    "under", "until", "up", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "whom", "why", "with", "would", "you", "your", "yours", "yourself",
}

TOKEN_RE = re.compile(r"<code>|<num>|[a-z][a-z'/\-]+")


def tokenize(sentence: str) -> List[str]:
    s = sentence.lower()
    s = re.sub(r"\b\d{4,5}[tu]?\b", " <code> ", s)
    s = re.sub(r"\b\d+\b", " <num> ", s)
    return TOKEN_RE.findall(s)


def grams(tokens: List[str], n: int) -> Set[str]:
    if len(tokens) < n:
        return set()
    out = set()
    for i in range(len(tokens) - n + 1):
        g = " ".join(tokens[i:i + n])
        toks = g.split()
        if all(t in STOPWORDS for t in toks if not t.startswith("<")):
            continue
        out.add(g)
    return out


def categorize(gram: str) -> str:
    for cat, rx in CATEGORY_PATTERNS:
        if rx.search(gram):
            return cat
    return "general_policy"


def polarity(gram: str) -> str:
    if re.search(r"\b(deni\w*|not|non-|without|prohibit\w*|inappropriate|exceed)\b", gram, re.I):
        return "negative"
    if re.search(r"\b(reimburse|allow|payable|appropriate|covered|approved)\b", gram, re.I):
        return "positive"
    return "neutral"


# ----------------------------------------------------------------------------
# Mining
# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="extraction_rules.json")
    ap.add_argument("--out", default="rule_keywords.csv")
    ap.add_argument("--min-support", type=int, default=2)
    ap.add_argument("--min-policies", type=int, default=1)
    ap.add_argument("--min-precision", type=float, default=0.25)
    ap.add_argument("--min-lift", type=float, default=1.2)
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isabs(args.input) and not os.path.exists(args.input):
        args.input = os.path.join(base_dir, args.input)
    if not os.path.isabs(args.out):
        args.out = os.path.join(base_dir, args.out)

    policies = load_policies(args.input)
    rule_sents: List[Tuple[str, int]] = []
    non_sents: List[Tuple[str, int]] = []

    for idx, pol in enumerate(policies):
        for unit in flatten_policy(pol, idx):
            kept = [s for s in split_sentences(unit.text) if not is_boilerplate(s)]
            for sent in kept:
                if _RULE_RE.search(sent):
                    rule_sents.append((sent, idx))
                else:
                    non_sents.append((sent, idx))

    n_rule = len(rule_sents)
    total = len(rule_sents) + len(non_sents)
    baseline = n_rule / max(total, 1)

    df_rule: Counter = Counter()
    df_non: Counter = Counter()
    pol_seen: Dict[str, Set[int]] = defaultdict(set)

    for sent, pidx in rule_sents:
        toks = tokenize(sent)
        found = set()
        for n in (1, 2, 3):
            found |= grams(toks, n)
        for g in found:
            df_rule[g] += 1
            pol_seen[g].add(pidx)

    for sent, _ in non_sents:
        toks = tokenize(sent)
        found = set()
        for n in (1, 2, 3):
            found |= grams(toks, n)
        for g in found:
            df_non[g] += 1

    rows = []
    for gram, r in df_rule.items():
        if r < args.min_support or len(pol_seen[gram]) < args.min_policies:
            continue
        nr = df_non.get(gram, 0)
        precision_val = (r + 1) / (r + nr + 2)
        if precision_val < args.min_precision:
            continue
        lift_val = precision_val / max(baseline, 0.001)
        if lift_val < args.min_lift:
            continue

        coverage = math.log1p(r) / math.log1p(max(n_rule, 1))
        score = round(0.75 * precision_val + 0.25 * coverage, 4)
        cat = categorize(gram)

        rows.append({
            "keyword": gram.replace("<code>", "{code}").replace("<num>", "{n}"),
            "category": cat,
            "strength": score,
            "polarity": polarity(gram),
            "strength_band": "strong" if score >= 0.70 else ("moderate" if score >= 0.55 else "weak"),
            "signal_role": "rule_indicator",
            "ngram": len(gram.split()),
            "rule_sentences": r,
            "background_sentences": nr,
            "policies": len(pol_seen[gram]),
            "precision": round(precision_val, 4),
            "lift": round(lift_val, 2) if lift_val != float("inf") else "inf",
        })

    # Sort rows by strength
    rows.sort(key=lambda r: (-r["strength"], -r["rule_sentences"], r["keyword"]))

    cols = ["keyword", "category", "strength", "strength_band", "polarity", "signal_role",
            "ngram", "rule_sentences", "background_sentences", "policies", "precision", "lift"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"Mined {len(rows)} signal keywords -> {args.out}")


if __name__ == "__main__":
    main()
