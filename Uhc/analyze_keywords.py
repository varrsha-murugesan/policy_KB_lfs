"""
analyze_keywords.py  --  Stage 1 of the payer policy rule-intelligence pipeline.

Reads extraction_rules.json, splits every piece of policy prose into sentences,
weakly labels each sentence as rule-bearing / not rule-bearing using a small set
of high-precision anchors, then mines 1-3 grams and keeps the ones that are
statistically over-represented in rule-bearing sentences.

Output: rule_keywords.csv  (keyword, category, strength, + supporting stats)

The point is that the lexicon is *discovered from this corpus*, not hand-typed,
so it transfers when new policies are added. The seed anchors below are only
used for labelling - they are deliberately narrow and high precision.

Usage:
    python analyze_keywords.py --input extraction_rules.json --out rule_keywords.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

from policy_lib import (flatten_policy, is_boilerplate, load_policies, norm_ws,
                        split_sentences)

# ----------------------------------------------------------------------------
# Weak labelling anchors
# ----------------------------------------------------------------------------

RULE_ANCHORS = [
    r"\bwill (?:not )?(?:be )?reimburse",
    r"\bwill not (?:consider|allow|pay|separately)",
    r"\b(?:is|are) not (?:separately |eligible for |considered )?reimbursab",
    r"\b(?:is|are) not eligible for (?:separate )?reimbursement",
    r"\b(?:is|are) eligible for (?:separate )?reimbursement",
    r"\bnot (?:be )?(?:separately )?(?:payable|reimbursed|allowed)",
    r"\bdo(?:es)? not (?:report|bill|submit|allow|reimburse|cover|apply)",
    r"\bshould (?:not )?be (?:reported|billed|submitted|appended|used)",
    r"\bshould (?:not )?(?:be )?report",
    r"\bmust be (?:reported|billed|submitted|appended|used|accompanied|present)",
    r"\bmay (?:not )?be (?:reported|billed|reimbursed|submitted|separately)",
    r"\bmay only be report",
    r"\bonly once (?:per|during|within)",
    r"\bonce per (?:day|month|year|calendar|patient|member|date|episode|lifetime)",
    r"\bmaximum of \w+ unit", r"\blimited to \w+",
    r"\bconsidered (?:incidental|inclusive|included|part of|integral|mutually exclusive)",
    r"\bis considered (?:a component|content of)",
    r"\bwill (?:be )?den(?:y|ied)", r"\bwill be denied",
    r"\brequires? (?:the use of )?modifier",
    r"\bappend(?:ed|ing)? (?:the )?modifier",
    r"\bnot separately (?:reimbursed|payable|reportable|billable)",
    r"\breimburses? (?:at|only|the)", r"\bwill allow",
    r"\bis (?:not )?(?:a )?separately reimbursable",
    r"\bdoes not (?:apply|recognize|separately)",
    r"\bshall (?:not )?be",
    r"\b(?:is|are) the reimbursable service",
    r"\bonly [^.]{0,60}\bis (?:the )?reimbursab",
    r"\bno modifiers? will override",
    r"\bis considered inclusive",
    r"\bmay only report",
    r"\bmust not be",
    r"\bis limited to\b",
    r"\ballowed per\b",
    r"\bmaximum (?:of|frequency|units?)",
    r"\bnot allowed\b",
    r"\b(?:adjust|adjustments?|downcod\w*|reduc\w*) to (?:the |a )?level \d\b",
    r"\boptum emergency department claim\b",
    r"\bstatus (?:code|indicator) indicates\b",
    r"\bused for reporting purposes only\b",
    r"\balign with cms when addressing the use of modifier\b",
    r"\bsplit (?:or shared) visit\b",
    r"\bincident-?to\b",
    r"\bclaim adjustments? or denials?\b",
    r"\bmay experience adjustments?\b",
    r"\bexclude facility claims from being subject\b",
]
_RULE_RE = re.compile("|".join(RULE_ANCHORS), re.I)

# Sentences that match no rule anchor form the background class. These are the
# obvious descriptive/definitional ones - kept only for reporting.
NONRULE_ANCHORS = [
    r"^according to (?:cms|the ama|cpt)[^,]{0,60}$",
    r"\bwas (?:developed|created|established)\b",
    r"\bfor (?:additional |more )?information\b",
    r"\bthis policy (?:describes|addresses|applies to all)\b",
    r"\bplease (?:see|visit|contact|note)\b",
    r"\bdefine[sd]? \w+ as\b", r"\bis defined as\b",
    r"\bwebsite\b", r"\bcopyright\b", r"\bpolicy version\b",
    r"\bfor the purposes of this policy\b", r"\blist of\b",
]
_NONRULE_RE = re.compile("|".join(NONRULE_ANCHORS), re.I)

# Only true function words. Negation, deontics and quantifiers are deliberately
# NOT stopwords - they carry most of the rule signal in payer prose.
STOPWORDS = set("""a an the of to in for on at by with and or is are was were be been being
this that these those it its as from such then so but which who whom whose where while into
have has had do does did their there they them he she his her you your we our us i""".split())

# ----------------------------------------------------------------------------
# Category assignment (ordered - first match wins, specific before generic)
# ----------------------------------------------------------------------------

CATEGORY_PATTERNS: List[Tuple[str, str]] = [
    ("exception_override", r"does not apply|do not apply|not apply|\binstead\b|in lieu of|"
                           r"exception|rather than|override|not subject to|other than|"
                           r"\bunless\b|\bexcept\b|\bhowever\b"),
    ("frequency_limit",    r"\bonce\b|per day|per month|per year|per week|per calendar|per date|"
                           r"maximum|more than|limited to|no more than|up to|frequency|"
                           r"\bunits?\b|lifetime|per member|per patient|\bsingle\b|one unit|"
                           r"time span|calendar month|time period|monthly|weekly|annual|"
                           r"\bday\b|\bdays\b|\bhours?\b|\bmonths?\b|\byears?\b|\bper\b"),
    ("bundling_inclusion", r"inclusive|included|incidental|integral|component|comprehensive|"
                           r"bundl|separate|separately|part of|global|"
                           r"mutually exclusive|content of|duplicate|in addition to"),
    ("modifier_usage",     r"modifier"),
    ("denial_prohibition", r"will not|\bnot be\b|not reimburs|not eligible|not payable|not allowed|"
                           r"deny|denied|denial|ineligible|do not|does not|\bnever\b|\bnot\b"),
    ("reimbursement_action", r"reimburs|payment|payable|\bpay\b|allowable|allowed amount|"
                             r"\bconsider\b|consider for|percent|rate|fee schedule|\ballow"),
    ("eligibility_condition", r"\bwhen\b|\bif\b|\bonly\b|provided that|as long as|"
                              r"in order to|requir|\bmust\b|\bshould\b|\bshall\b|\bmay\b|"
                              r"eligible|appropriate|criteria|\bcondition"),
    ("coding_requirement",  r"report|bill|submit|append|claim|\bcpt\b|hcpcs|\bcode|procedure|"
                            r"description|guideline|category|modifier|units of service"),
    ("entity_scope",        r"same group|same individual|same physician|same provider|"
                            r"same member|same patient|same specialt|same date|same day|"
                            r"tax identification|health care professional|qualified|"
                            r"physician|provider|member|patient|beneficiar|\bgroup\b|"
                            r"\bsame\b|\bindividual\b"),
    ("temporal_scope",      r"date of service|dates of service|calendar|\bdate\b|\btime\b|"
                            r"\bperiod\b|\bepisode\b|\bsession\b|\bencounter\b|\bvisit\b"),
    ("documentation",       r"documentation|medical record|records|documented|\bnotes?\b|chart"),
    ("place_of_service",    r"place of service|\bpos\b|facility|non-facility|setting|"
                            r"outpatient|inpatient|office|home\b"),
]
_CATEGORY_RE = [(name, re.compile(pat, re.I)) for name, pat in CATEGORY_PATTERNS]

# Grams with no function/deontic/action word are subject-matter vocabulary that
# happens to co-occur with rules (e.g. "specimen"); they are kept but flagged so
# the extractor can weight them near zero.
_ACTIONISH = re.compile(
    r"\b(will|shall|must|may|should|can|do|does|not|no|only|once|per|when|if|unless|"
    r"except|instead|report|bill|submit|reimburs|pay|allow|deny|denied|append|use|used|"
    r"consider|require|eligible|separate|separately|include|included|inclusive|"
    r"incidental|limit|limited|maximum|minimum|same|each|both|additional|"
    r"modifier|units?|frequency|apply|applies|applied|subject|override|"
    r"appropriate|based|following|exceed|greater|less)\b", re.I)


_NEG = re.compile(r"\bnot\b|\bno\b|\bnever\b|\bdeny\b|denied|denial|ineligible|"
                  r"\bwithout\b|\bexclud|\bunless\b|\bprohibit", re.I)
_POS = re.compile(r"\bwill reimburse|\breimburses?\b|\ballow|\beligible\b|\bmay be reimbursed\b|"
                  r"\bis reimbursable\b|\bwill be reimbursed\b|\bconsider for\b", re.I)


def polarity(gram: str) -> str:
    """Does the signal point toward a restriction or toward an allowance?"""
    if _NEG.search(gram):
        return "restrictive"
    if _POS.search(gram):
        return "permissive"
    return "neutral"


def categorize(gram: str) -> str:
    if not _ACTIONISH.search(gram):
        return "clinical_context"
    for name, rx in _CATEGORY_RE:
        if rx.search(gram):
            return name
    return "general_policy"


# ----------------------------------------------------------------------------
# Mining
# ----------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[a-z][a-z'\-]+|\d+%|\bnot\b")


def tokenize(sentence: str) -> List[str]:
    s = sentence.lower()
    s = re.sub(r"\b\d{4,5}[tu]?\b", " <code> ", s)   # collapse codes so grams generalize
    s = re.sub(r"\b\d+\b", " <num> ", s)
    return re.findall(r"<code>|<num>|[a-z][a-z'/\-]+", s)


def grams(tokens: List[str], n: int) -> Set[str]:
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def informative(gram: str) -> bool:
    parts = gram.split()
    if all(p in STOPWORDS or p in ("<code>", "<num>") for p in parts):
        return False
    if parts[0] in ("<code>", "<num>") or parts[-1] in ("<code>", "<num>"):
        return False
    if len(gram) < 4:
        return False
    return True


_EXC_VOCAB = re.compile(r"instead|however|except|unless|exception|rather|in lieu|"
                        r"does not apply|do not apply|not apply|other than|notwithstanding", re.I)


_LEAD_OK = STOPWORDS | set("""not no only when if unless except instead however also
additionally therefore thus similarly otherwise although though while note each both any all
other another per after before once under during payment reimbursement modifiers codes
services claims""".split())


def mine_linkage_cues(sequences: List[Tuple[int, List[str]]], min_support: int,
                      min_policies: int = 3) -> List[dict]:
    """
    Discover sentence-initial phrases that mark a sentence as a continuation of
    the rule stated just before it ("Instead, ...", "This aligns with ...",
    "No modifiers will override ..."). These are what let the extractor glue a
    rule and its override together instead of emitting two orphan sentences.
    """
    follows: Counter = Counter()
    total: Counter = Counter()
    seen_in: Dict[str, Set[int]] = defaultdict(set)
    n_after_rule = n_total = 0

    for pidx, sents in sequences:
        for i, sent in enumerate(sents):
            if i == 0:
                continue
            toks = tokenize(sent)
            if not toks or toks[0] not in _LEAD_OK:
                continue
            prev_is_rule = bool(_RULE_RE.search(sents[i - 1]))
            n_total += 1
            n_after_rule += int(prev_is_rule)
            for n in (1, 2, 3):
                if len(toks) >= n:
                    lead = " ".join(toks[:n])
                    total[lead] += 1
                    if prev_is_rule:
                        follows[lead] += 1
                        seen_in[lead].add(pidx)

    baseline = n_after_rule / max(n_total, 1)
    rows = []
    for lead, tot in total.items():
        f = follows.get(lead, 0)
        if f < min_support or len(seen_in[lead]) < min_policies:
            continue
        precision = (f + 1) / (tot + 2)
        lift = precision / baseline if baseline else 0
        if lift < 1.25:
            continue
        score = round(0.75 * precision + 0.25 * math.log1p(f) / math.log1p(max(n_after_rule, 2)), 4)
        rows.append({
            "keyword": lead.replace("<code>", "{code}").replace("<num>", "{n}"),
            "category": "exception_override" if _EXC_VOCAB.search(lead) else "continuation_cue",
            "strength": score,
            "strength_band": "strong" if score >= 0.72 else ("moderate" if score >= 0.62 else "weak"),
            "polarity": polarity(lead),
            "signal_role": "linkage_cue",
            "ngram": len(lead.split()),
            "rule_sentences": f,
            "background_sentences": tot - f,
            "policies": len(seen_in[lead]),
            "precision": round(precision, 4),
            "lift": round(lift, 2),
        })
    rows.sort(key=lambda r: -r["strength"])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="extraction_rules.json")
    ap.add_argument("--out", default="rule_keywords.csv")
    ap.add_argument("--min-support", type=int, default=5)
    ap.add_argument("--min-policies", type=int, default=2)
    ap.add_argument("--min-precision", type=float, default=0.35)
    ap.add_argument("--min-lift", type=float, default=1.5)
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(args.input) and os.path.exists(os.path.join(base_dir, args.input)):
        args.input = os.path.join(base_dir, args.input)
    if not os.path.isabs(args.out):
        args.out = os.path.join(base_dir, args.out)

    policies = load_policies(args.input)

    rule_sents: List[Tuple[str, int]] = []
    non_sents: List[Tuple[str, int]] = []   # background: everything not anchored
    sequences: List[Tuple[int, List[str]]] = []
    descriptive = 0
    total = 0
    for idx, pol in enumerate(policies):
        for unit in flatten_policy(pol, idx):
            kept = [s for s in split_sentences(unit.text) if not is_boilerplate(s)]
            if len(kept) > 1:
                sequences.append((idx, kept))
            for sent in kept:
                total += 1
                if _RULE_RE.search(sent):
                    rule_sents.append((sent, idx))
                else:
                    non_sents.append((sent, idx))
                    if _NONRULE_RE.search(sent):
                        descriptive += 1

    n_rule, n_non = len(rule_sents), len(non_sents)
    baseline = n_rule / max(total, 1)
    print(f"sentences: {total}   rule-anchored: {n_rule}   background: {n_non} "
          f"(of which clearly descriptive: {descriptive})   baseline P(rule)={baseline:.3f}")

    df_rule: Counter = Counter()
    df_non: Counter = Counter()
    pol_seen: Dict[str, Set[int]] = defaultdict(set)

    for sent, pidx in rule_sents:
        toks = tokenize(sent)
        found = set()
        for n in (1, 2, 3):
            found |= grams(toks, n)
        for g in found:
            if informative(g):
                df_rule[g] += 1
                pol_seen[g].add(pidx)
    for sent, _ in non_sents:
        toks = tokenize(sent)
        found = set()
        for n in (1, 2, 3):
            found |= grams(toks, n)
        for g in found:
            if informative(g):
                df_non[g] += 1

    rows = []
    for gram, r in df_rule.items():
        if r < args.min_support or len(pol_seen[gram]) < args.min_policies:
            continue
        nr = df_non.get(gram, 0)
        # P(sentence is rule-bearing | sentence contains gram), with add-1 smoothing
        precision = (r + 1) / (r + nr + 2)
        if precision < args.min_precision:
            continue
        lift = precision / baseline
        if lift < args.min_lift:
            continue
        # strength blends how discriminative the gram is with how often it fires
        coverage = math.log1p(r) / math.log1p(n_rule)
        score = round(0.75 * precision + 0.25 * coverage, 4)
        rows.append({
            "keyword": gram.replace("<code>", "{code}").replace("<num>", "{n}"),
            "category": categorize(gram),
            "strength": score,
            "polarity": polarity(gram),
            "strength_band": "strong" if score >= 0.72 else ("moderate" if score >= 0.62 else "weak"),
            "ngram": len(gram.split()),
            "rule_sentences": r,
            "background_sentences": nr,
            "policies": len(pol_seen[gram]),
            "precision": round(precision, 4),
            "lift": round(lift, 2) if lift != float("inf") else "inf",
        })

    # Prune a shorter gram only when it is essentially never seen outside the
    # longer one (same support) and no more discriminative - i.e. it is redundant.
    by_key = {r["keyword"]: r for r in rows}
    drop = set()
    for k, row in by_key.items():
        toks = k.split()
        if len(toks) == 1:
            continue
        parts = [toks[0], toks[-1], " ".join(toks[:2]), " ".join(toks[1:])] if len(toks) == 3 \
            else [toks[0], toks[-1]]
        for part in parts:
            other = by_key.get(part)
            if other and other["rule_sentences"] <= row["rule_sentences"] * 1.05 \
                    and other["precision"] <= row["precision"]:
                drop.add(part)
    rows = [r for r in rows if r["keyword"] not in drop]

    for r in rows:
        r["signal_role"] = "rule_indicator"
    # subject-matter vocabulary is only kept when it is a strong signal; weak
    # clinical nouns add nothing but noise to the extractor
    rows = [r for r in rows
            if r["category"] != "clinical_context" or r["strength_band"] != "weak"]

    cues = mine_linkage_cues(sequences, max(args.min_support // 2, 3))
    existing = {r["keyword"] for r in rows}
    cues = [c for c in cues if c["keyword"] not in existing]
    print(f"linkage cues discovered: {len(cues)}")
    rows.extend(cues)

    rows.sort(key=lambda r: (r["signal_role"] != "rule_indicator",
                             -r["strength"], -r["rule_sentences"], r["keyword"]))

    cols = ["keyword", "category", "strength", "strength_band", "polarity", "signal_role",
            "ngram", "rule_sentences", "background_sentences", "policies", "precision", "lift"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} keywords -> {args.out}")
    cat = Counter(r["category"] for r in rows)
    for c, n in cat.most_common():
        print(f"  {c:24s} {n}")


if __name__ == "__main__":
    main()
