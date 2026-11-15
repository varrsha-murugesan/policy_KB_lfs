#!/usr/bin/env python3
"""
rule_core.py
============
Shared, deterministic rule-selection engine used by both payer scripts
(generate_billing_rules_humana.py and generate_billing_rules_uhc.py).

Keep this file next to the two scripts. Both payers go through the SAME
keyword lexicon, sentence grouping, rule clubbing and output schema, so the
two rule sets can be compared category-by-category and code-by-code.

Design principles
-----------------
1. VERBATIM: rule text is always policy text (only whitespace / unicode is
   normalised). Nothing is paraphrased or generated into Policy_Rule_Text.
2. COMPLETE: a rule is a complete thought. A sentence is never split from its
   lead-in ("... in the following situations:"), its bullet list, or its
   dependent follow-ups ("No modifiers will override this denial.").
   Short sections are kept whole; long sections are cut only at
   group boundaries.
3. KEYWORD-DRIVEN: a block becomes a rule only if it contains at least one
   DIRECTIVE keyword (reimburse / deny / not separately / once per / must ...).
   Topic keywords (age, POS, diagnosis, add-on ...) only refine the category.
4. CLUBBED: the same rule is stated ONCE per policy. Rules whose text is
   identical after masking the codes are merged and their codes unioned.
   Code lists from tables/attachments are attached to the rule they belong
   to (Target_Codes + Rule_Code_Map) instead of creating one rule per code
   or per attachment value.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import sql_skeleton

# ==============================================================================
# 1. TEXT NORMALISATION
# ==============================================================================

_UNICODE_MAP = {
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "–", "\u2014": "–", "\u2011": "-", "\u00a0": " ",
    "\ufffd": "-", "\u2192": "->", "\u273d": "•", "\u25aa": "▪",
    "\u25cf": "•", "\uf0b7": "•", "\uf0a7": "▪", "\u2022": "•",
}


def clean_text(text: Optional[str]) -> str:
    """Whitespace/unicode normalisation only. Never changes wording."""
    if not text:
        return ""
    text = str(text)
    # soft hyphen used as a sub-bullet marker in some PDFs
    text = re.sub(r"(^|\s)\u00ad\s", r"\1◦ ", text)
    text = text.replace("\u00ad", "")
    for k, v in _UNICODE_MAP.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    # "IS NOT REIMBURSABLE ." -> "IS NOT REIMBURSABLE."
    text = re.sub(r"\s+([.,;:])(?=\s|$)", r"\1", text)
    return text.strip()


def norm_key(text: str) -> str:
    """Comparison key: lower case, alphanumerics only."""
    t = clean_text(text).lower()
    t = re.sub(r"[^a-z0-9%+]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ==============================================================================
# 2. CODES & MODIFIERS
# ==============================================================================

# CPT (5 digits), CPT Cat II/III & PLA (4 digits + F/T/U/M), HCPCS/CDT (letter + 4 digits)
_CODE_BODY = r"(?:\d{5}|\d{4}[FTUM]|[ABCDEGHJKLMPQRSTUV]\d{4})"
CODE_RE = re.compile(rf"(?<![A-Za-z0-9/$.,]){_CODE_BODY}(?![A-Za-z0-9])")
CODE_FULL_RE = re.compile(rf"^\+?{_CODE_BODY}$")
RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9])\+?({_CODE_BODY})\s*(?:-|–|through|thru|to)\s*\+?({_CODE_BODY})(?![A-Za-z0-9])",
    re.I,
)
PLACEHOLDER_CODE_RE = re.compile(r"\b(?:([a-zA-Z])\1{4}|X{3,5}|[A-Z]{5,11})\b")


def is_code(tok: str) -> bool:
    return bool(CODE_FULL_RE.match(tok.strip()))


def _expand_range(a: str, b: str, max_span: int = 400) -> List[str]:
    if a.isdigit() and b.isdigit():
        ia, ib = int(a), int(b)
        if 0 < ib - ia <= max_span:
            return [f"{i:05d}" for i in range(ia, ib + 1)]
    elif a[0].isalpha() and a[0] == b[0] and a[1:].isdigit() and b[1:].isdigit():
        ia, ib = int(a[1:]), int(b[1:])
        if 0 < ib - ia <= max_span:
            return [f"{a[0]}{i:04d}" for i in range(ia, ib + 1)]
    elif a[-1].isalpha() and a[-1] == b[-1] and a[:4].isdigit() and b[:4].isdigit():
        ia, ib = int(a[:4]), int(b[:4])
        if 0 < ib - ia <= max_span:
            return [f"{i:04d}{a[-1]}" for i in range(ia, ib + 1)]
    return [a, b]


def extract_codes(text: str) -> List[str]:
    """Ordered unique CPT/HCPCS codes, with ranges (93224-93227) expanded."""
    out: "OrderedDict[str, None]" = OrderedDict()
    spans = []
    for m in RANGE_RE.finditer(text):
        for c in _expand_range(m.group(1).upper(), m.group(2).upper()):
            out[c] = None
        spans.append(m.span())
    for m in CODE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        out[m.group(0).upper()] = None
    return list(out)


def code_sort_key(c: str):
    return (0 if c[:1].isdigit() else 1, c)


def compress_codes(codes: Iterable[str]) -> str:
    """'93224, 93225, 93226, 93227, 99291' -> '93224-93227, 99291' (display only)."""
    codes = sorted(set(codes), key=code_sort_key)
    out, run = [], []

    def _num(c):
        if c.isdigit():
            return ("N", int(c))
        if c[0].isalpha() and c[1:].isdigit():
            return (c[0], int(c[1:]))
        if c[-1].isalpha() and c[:-1].isdigit():
            return ("S" + c[-1], int(c[:-1]))
        return (c, None)

    def _flush():
        if not run:
            return
        out.append(run[0] if len(run) < 3 else f"{run[0]}-{run[-1]}")
        if 1 < len(run) < 3:
            out.extend(run[1:])

    prev = None
    for c in codes:
        k = _num(c)
        if prev and k[1] is not None and prev[0] == k[0] and prev[1] is not None and k[1] == prev[1] + 1:
            run.append(c)
        else:
            _flush()
            run = [c]
        prev = k
    _flush()
    return ", ".join(out)


KNOWN_MODIFIERS = set("""
22 23 24 25 26 27 32 33 47 50 51 52 53 54 55 56 57 58 59 62 63 66 73 74 76 77 78 79 80 81 82 90 91 92 93 95 96 97 99
AA AD AF AG AH AI AJ AK AM AO AQ AR AS AT AU AV AW AX AY AZ BA BL BO BP BR BU CA CB CG CO CQ CR CS CT
E1 E2 E3 E4 EA EB EC ED EE EJ EM EP ER ET EX EY F1 F2 F3 F4 F5 F6 F7 F8 F9 FA FB FC FP FQ FR FS FT FX FY
G0 G1 G2 G3 G4 G5 G6 G7 G8 G9 GA GB GC GE GF GG GH GJ GK GL GM GN GO GP GQ GR GS GT GU GV GW GX GY GZ
HA HB HC HD HE HF HG HH HI HJ HK HL HM HN HO HP HQ HR HS HT HU HV HW HX HY HZ
J1 J2 J3 J4 JA JB JC JD JE JG JW JZ K0 K1 K2 K3 K4 KA KB KC KD KE KF KH KI KJ KK KL KM KN KO KP KQ KR KS KT KU KV KW KX KY KZ
LC LD LL LM LR LS LT LU M2 MS NB NR NU P1 P2 P3 P4 P5 P6 PA PB PC PD PI PL PM PN PO PS PT
Q0 Q1 Q2 Q3 Q4 Q5 Q6 Q7 Q8 Q9 QA QB QC QD QE QF QG QH QJ QK QL QM QN QP QQ QR QS QT QW QX QY QZ
RA RB RC RD RE RI RR RT SA SB SC SD SE SF SG SH SJ SK SL SM SN SQ SS ST SU SV SW SY
T1 T2 T3 T4 T5 T6 T7 T8 T9 TA TB TC TD TE TF TG TH TJ TK TL TM TN TP TQ TR TS TT TU TV TW
U1 U2 U3 U4 U5 U6 U7 U8 U9 UA UB UC UD UE UF UG UH UJ UK UN UP UQ UR US V1 V2 V3 V5 V6 V7 VM VP
X1 X2 X3 X4 X5 XE XP XS XU
""".split())

_MOD_LIST_RE = re.compile(
    r"(?i:\bmodifiers?)\s*[:\-]?\s*((?:-?[A-Z0-9]{2}\b(?:\s*(?:,|/|&|\bor\b|\band\b|–|-)\s*)?){1,12})"
)
_MOD_SUFFIX_RE = re.compile(r"\b([A-Z0-9]{2})\s+(?i:modifiers?)\b")
_MOD_RANGE_RE = re.compile(r"\b([A-Z])(\d)\s*[–-]\s*\1(\d)\b")
_MOD_PROHIBIT_RE = re.compile(
    r"\b(?:do not report|not be reported|should not be (?:reported|appended|used)|is not allowed|are not allowed|"
    r"not recognized|does not recognize|do not recognize|not reimburs\w*|informational only|no modifiers?|"
    r"inappropriate|not appropriate|do not allow|does not allow|will not override|not override|invalid)\b",
    re.I,
)
_MOD_REQUIRE_RE = re.compile(
    r"\b(?:must|required|requires|require|should be (?:reported|appended|used|billed)|report (?:the )?\w*\s*(?:code )?with|"
    r"append(?:ed)?|is to be used|are to be used)\b",
    re.I,
)


def extract_modifiers(text: str) -> List[str]:
    mods: "OrderedDict[str, None]" = OrderedDict()
    if "X{EPSU}" in text or "X {EPSU}" in text:
        for m in ("XE", "XP", "XS", "XU"):
            mods[m] = None
    for m in _MOD_LIST_RE.finditer(text):
        seg = m.group(1)
        for a, d1, d2 in _MOD_RANGE_RE.findall(seg):
            for i in range(int(d1), int(d2) + 1):
                mods[f"{a}{i}"] = None
        for tok in re.findall(r"-?\b([A-Z0-9]{2})\b", seg):
            if tok in KNOWN_MODIFIERS:
                mods[tok] = None
    for tok in _MOD_SUFFIX_RE.findall(text):
        if tok in KNOWN_MODIFIERS:
            mods[tok] = None
    return list(mods)


def classify_modifiers(text: str, mods: List[str]) -> Tuple[List[str], List[str]]:
    """Sentence-level: a modifier is PROHIBITED / REQUIRED by the sentence it appears in."""
    req, pro = set(), set()
    text = _MOD_RANGE_RE.sub(lambda m: ", ".join(f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)),
                             text)
    for s in re.split(r"(?<=[.;:])\s+|\s(?=[•▪])", text):
        found = [m for m in mods if re.search(rf"(?<![A-Z0-9]){re.escape(m)}(?![A-Z0-9])", s)]
        if not found:
            continue
        if _MOD_PROHIBIT_RE.search(s):
            # "Do not report modifier 22 with an E/M service" restricts the context, it does not ban 22
            if _MOD_CONTEXT_RE.search(s) and not re.search(r"informational|not recognized|does not recognize|"
                                                            r"not reimburs|not allowed|no modifier", s, re.I):
                continue
            pro.update(found)
        elif _MOD_REQUIRE_RE.search(s):
            req.update(found)
    req -= pro
    return [m for m in mods if m in req], [m for m in mods if m in pro]


_MOD_CONTEXT_RE = re.compile(
    r"(?:do not report|not be reported|should not be (?:reported|appended|used))\s+(?:the\s+)?(?:an?\s+)?"
    r"(?:\w+\s+){0,3}?modifiers?\s+[A-Z0-9]{2}(?:\s*(?:,|or|and)\s*[A-Z0-9]{2})*\s+(?:with|on|for|when|in|to|after|"
    r"if|unless|by)\b", re.I)


BILL_TYPE_RE = re.compile(r"\b(?:bill types?|type of bill|TOB)\s*((?:0?\d{2}[0-9X](?:\s*(?:,|or|and|–|-)\s*)?)+)", re.I)


def extract_bill_types(text: str) -> List[str]:
    out = OrderedDict()
    for m in BILL_TYPE_RE.finditer(text):
        for bt in re.findall(r"0?\d{2}[0-9X]", m.group(1), re.I):
            out[bt.upper()] = None
    return list(out)


# ==============================================================================
# 3. KEYWORD LEXICON  (edit here to tune rule selection)
# ==============================================================================
# DIRECTIVE categories: at least one must fire for a block to be a rule.
# TOPIC categories: only refine the category of a block that is already a rule.

KEYWORD_LEXICON: "OrderedDict[str, Tuple[bool, str]]" = OrderedDict([
    ("TIME_SPAN_LIMIT", (True,
        r"\btime span\b|specifies a time period|once during (?:that|the|each|a) (?:time )?period|"
        r"\b(?:30|31|60|90) days must pass|within \d+ (?:calendar )?days (?:of|after|from)")),
    ("ONCE_IN_A_LIFETIME", (True,
        r"\bonce (?:per|in a|in the|during (?:a|the)(?: patient'?s?)?) lifetime\b|\b(?:per|in a) lifetime\b|"
        r"\blifetime (?:limit|maximum)\b|\bcode family\b")),
    ("ADD_ON_PRIMARY", (False,
        r"\badd-?\s?on (?:codes?|procedures?|services?)\b|\bAOCs?\b|\bprimary (?:procedure|service|code)s?\b")),
    ("BUNDLING_INCLUSION", (True,
        r"\b(?:re-?|un)?bundl(?:ed|es|ing)\b|(?<!all-)\binclusive\b|\bincidental (?:to|services?)\b|\bintegral (?:to|part)\b|"
        r"\b(?:is|are) (?:a )?components? of\b|\bincluded in (?:the )?(?:global|payment|reimbursement|allowance|"
        r"facility|rate|primary|comprehensive|package|surgical)\b|\bnot (?:be )?separately (?:reimburs\w*|payable|paid|"
        r"billable|reportable|allowed|recognized)\b|\b(?:separate|separately) (?:reimbursement|payment)\b|"
        r"\bseparately reimbursable\b|\bmutually exclusive\b|\bmost comprehensive\b|\bglobal (?:surgical )?"
        r"(?:package|period|days?)\b|\bNCCI\b|\bPTP\b|\bon the same (?:date of service|day|claim) as\b")),
    ("FREQUENCY_LIMIT", (True,
        r"\bonce (?:per|a|every|each)\b|\bone (?:per|unit per|time per)\b|\bper (?:date of service|day|calendar "
        r"(?:day|week|month|year)|month|year|week|session|encounter|visit|episode|admission)\b|\bevery \d+ "
        r"(?:days|weeks|months|years)\b|\bmaximum (?:of )?\d+|\bmax(?:imum)? (?:number of )?(?:allowed )?units?\b|"
        r"\bunits? (?:of service )?(?:allowed|limit)|\bunit limit|\bno more than\b|(?<!not )\blimited to (?:\d|one|two|a maximum|once)|\bnot to exceed\b|"
        r"\bfrequency\b|\bmedically unlikely\b|\bMUEs?\b|\bdaily limit\b|\bmore than (?:once|one|\d+)\b|\bonly one\b|"
        r"\bsingle (?:most comprehensive|unit)\b|\b\d+[- ]month rental\b")),
    ("AGE_GENDER_RESTRICTION", (False,
        r"\b\d+\s*(?:years?|months?|days?)(?:\s*(?:of age|old)|\s+(?:and|or) (?:older|younger))|\bage (?:designation|"
        r"range|limit|restriction|edit|category)s?\b|\bpatient'?s? age\b|\boutside (?:of )?the age\b|\bage-based\b|"
        r"\b(?:pediatric|neonatal|newborn|adolescent)s?\b|\b(?:younger|older) than \d+|\bunder (?:the )?age (?:of )?\w+|"
        r"\bover the age of\b|\bage (?:is )?(?:less|greater|more|younger|older) than\b|\b(?:aged?|ages) \d+\b|"
        r"(?<!trans)\bgender\b(?![- ]?(?:dysphori|incongruen|affirm|identit|transition|reassign|non-?conform|diverse|expression))|"
        r"\b(?:males?|females?)\b")),
    ("PLACE_OF_SERVICE", (False,
        r"\bplace of service\b|\bPOS\b|\bnon-?facility\b|\bfacility setting\b|\boffice setting\b|\bsetting of care\b")),
    ("BILL_TYPE_REVENUE_CODE", (False,
        r"\btype of bill\b|\bbill types?\b|\bTOB\b|\brevenue codes?\b|\bUB-?04\b|\bcondition codes?\b|\boccurrence codes?\b")),
    ("DIAGNOSIS_REQUIREMENT", (False,
        r"\bdiagnosis codes?\b|\bICD-10(?:-CM)?\b|\bprimary diagnosis\b|\b(?:supporting|covered|appropriate|qualifying) "
        r"diagnos[ie]s\b|\bdiagnoses of\b")),
    ("PRIOR_AUTHORIZATION", (True,
        r"\bprior authorization\b|\bpre-?authorization\b|\bprecertification\b|\breferral (?:is )?required\b")),
    ("PAYMENT_ADJUSTMENT", (True,
        r"\b\d+(?:\.\d+)?\s?(?:%|percent)\b|\bpayment reduction\b|\breduc(?:ed|tion|es)\b|\blesser of\b|"
        r"\bamount allowed\b|\ballowed amount\b|\bfee schedule\b|\bcontracted rate\b|\bconversion factor\b|"
        r"\bMPPR\b|\bmonthly rental rate\b")),
    ("MODIFIER_RULE", (True,
        r"\b(?:report|append|bill|submit|use)(?:ed|s|ing)? (?:\w+ ){0,4}(?:with )?(?:the |a |an )?(?:appropriate )?"
        r"modifiers?\b|\bmodifiers? [A-Z0-9]{2}\b[^.]{0,80}\b(?:must|should|is|are|will|may|not|required|"
        r"override|informational|allowed|recognized|appended)\b|\bdoes not recognize\b|\bno modifiers?\b|"
        r"\bmodifiers? (?:must|should|is required|are required|will not|override)|\brequires?\b[^.]{0,60}\bmodifiers?\b")),
    ("DENIAL_PROHIBITION", (True,
        r"\bwill not (?:be )?(?:reimburs\w*|paid|payable|allowed|covered|considered)|\b(?:does|do) not (?:reimburse|"
        r"allow|pay|cover)\b|\bnot (?:be )?(?:reimbursable|reimbursed|covered|payable|allowed|eligible)\b|"
        r"\bnon-?(?:reimbursable|covered|payable)\b|\b(?:will be|is|are|be) denied\b|\bden(?:y|ies)\b|\bdisallow\w*|"
        r"\b(?:may|should|must|can) not be (?:reported|billed|submitted|used|appended)\b|\bshould not be\b|"
        r"\bdo not report\b|\bnot (?:be )?reported\b|\bprohibited\b|\bnever (?:be )?reimbursed\b|"
        r"\binappropriate to (?:report|bill)\b|\bbilling errors?\b|\bnever events?\b|\bnot considered for reimbursement\b|"
        r"\b(?:is|are) excluded (?:from|in)\b")),
    ("REIMBURSEMENT_CRITERIA", (True,
        r"\bwill (?:be )?(?:reimburs\w*|considered for reimbursement|consider reimbursement|allow\w*|pay)\b|"
        r"\b(?:is|are) reimbursable\b|\breimburs\w* (?:only|when|if|for|at|the|a|separately)\b|\breimbursement "
        r"(?:is|will|may|for|of)\b|\beligible for (?:reimbursement|payment)\b|\bpayable\b|\bmay be (?:reimbursed|"
        r"billed|reported|allowed)\b|\b(?:must|should|shall)(?: only| also)? (?:be )?(?:reported|billed|submitted|appended|"
        r"included|documented|performed|ordered|used|accompanied|met|report|bill|submit|include)\b|"
        r"\b(?:is|are) required\b|\brequires?\b|\bonly (?:when|if|once|for)\b|\ballows?\b|\bconsistent with\b")),
    ("EXCEPTION_OVERRIDE", (True,
        r"\bdoes not apply\b|\bdo not apply\b|\bwill not apply\b|\b(?:is|are) not subject to\b|\bexcept(?:ions?)? "
        r"(?:when|for|to|in|that|those)\b|\bexempt\b|\bunless\b|\boverrides?\b|\bin lieu of\b|\bnotwithstanding\b")),
])

_LEX = OrderedDict((k, (d, re.compile(p, re.I))) for k, (d, p) in KEYWORD_LEXICON.items())
GENERIC_CATEGORIES = ("DENIAL_PROHIBITION", "REIMBURSEMENT_CRITERIA", "EXCEPTION_OVERRIDE")
CATEGORY_PRIORITY = list(KEYWORD_LEXICON.keys())


def keyword_hits(text: str) -> "OrderedDict[str, List[str]]":
    hits: "OrderedDict[str, List[str]]" = OrderedDict()
    for cat, (_, rx) in _LEX.items():
        found = [m.group(0) for m in rx.finditer(text)]
        if found:
            hits[cat] = found
    return hits


def has_directive(text: str) -> bool:
    return any(d and rx.search(text) for d, rx in _LEX.values())


TIER1 = ("TIME_SPAN_LIMIT", "ONCE_IN_A_LIFETIME", "ADD_ON_PRIMARY", "BUNDLING_INCLUSION", "AGE_GENDER_RESTRICTION",
         "PLACE_OF_SERVICE", "BILL_TYPE_REVENUE_CODE", "DIAGNOSIS_REQUIREMENT", "PRIOR_AUTHORIZATION")
TIER2 = ("FREQUENCY_LIMIT", "PAYMENT_ADJUSTMENT", "MODIFIER_RULE")


def classify(text: str) -> Tuple[str, List[str], List[str]]:
    """Returns (primary_category, secondary_categories, trigger_keywords).
    Tier 1 (specific edit type) > Tier 2 (limit / payment / modifier) > generic outcome.
    Within tier 1/2 the category with most keyword hits wins (ties -> lexicon order);
    generic outcomes use fixed order DENIAL > REIMBURSEMENT > EXCEPTION."""
    hits = keyword_hits(text)
    if not hits:
        return "POLICY_REQUIREMENT", [], []
    primary = None
    for tier in (TIER1, TIER2):
        cands = [c for c in hits if c in tier]
        if cands:
            primary = max(cands, key=lambda c: (len(hits[c]), -CATEGORY_PRIORITY.index(c)))
            break
    if primary is None:
        primary = next(c for c in GENERIC_CATEGORIES if c in hits)
    secondary = [c for c in hits if c != primary]
    kws = OrderedDict()
    for c in hits:
        for k in hits[c]:
            kws[k.lower().strip()] = None
    return primary, secondary, list(kws)[:12]


# ==============================================================================
# 4. BOILERPLATE
# ==============================================================================

BOILERPLATE_RE = re.compile(
    r"claims payments are subject to other plan requirements|as a medicare advantage plan, we have developed payment "
    r"policies|this document must be downloaded|click here|proprietary information of|registered trademark|"
    r"copyright \d{4}|all rights reserved|^this (?:reimbursement )?policy (?:is intended|applies to|does not address|"
    r"describes|outlines|establishes)|^the following policy applies to|uses a customized version of the optum|"
    r"is not a guarantee of|please see the definitions section|^(?:refer to|see) the .{0,40}section|"
    r"^for questions|contact (?:your|the) (?:provider|network)|^note: this policy|"
    r"^humana(?:'s)? (?:payment|reimbursement) policies? (?:is|are)|codes appearing in policy documents are included only|"
    r"^they may not be all-inclusive",
    re.I,
)

SKIP_SECTION_RE = re.compile(
    r"^(?:definitions?|definitions of italicized terms|revision|history|policy history|references?|resources|"
    r"disclaimer|overview|attachments?|contacts?|publications|change summary|summary of changes|background|purpose|"
    r"applicable codes?|state exceptions|related (?:policies|links))\b",
    re.I,
)


class CorpusBoilerplate:
    """Sentences repeated across many policies are boilerplate (e.g. the 'subject to
    other plan requirements' paragraph in every Humana policy)."""

    def __init__(self, min_policies: int = 5, share: float = 0.25):
        self.df: Counter = Counter()
        self.n_policies = 0
        self.min_policies, self.share = min_policies, share

    def add_policy(self, texts: Iterable[str]) -> None:
        self.n_policies += 1
        keys = set()
        for t in texts:
            for s, _ in split_pieces(t):
                k = norm_key(s)
                if len(k) >= 40:
                    keys.add(k)
        self.df.update(keys)

    def threshold(self) -> int:
        return max(self.min_policies, int(self.share * max(self.n_policies, 1)))

    def is_boilerplate(self, sentence: str) -> bool:
        if BOILERPLATE_RE.search(sentence.strip()):
            return True
        k = norm_key(sentence)
        # a repeated sentence is boilerplate only if it states no rule itself
        return (len(k) >= 40 and self.df.get(k, 0) >= self.threshold() and not extract_codes(sentence)
                and not has_directive(sentence))


# ==============================================================================
# 5. SENTENCE SPLITTING + COMPLETE-RULE GROUPING
# ==============================================================================

_ABBR_RE = re.compile(r"\b(e\.g|i\.e|etc|vs|Nos?|Dr|Mr|Ms|St|Inc|approx|Fig|U\.S|a\.m|p\.m|Pub|Ch|Sec|Rev|Jan|Feb|Mar|Apr|"
                      r"Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|LLC|Corp|Co)\.", re.I)
_BULLET_SPLIT_RE = re.compile(r"\s(?=[•▪◦●]\s?)|\s(?=[–-]\s(?=[A-Z](?:[a-z]|-[A-Za-z]|[A-Z]{2})))|\s(?=o\s(?=[A-Z][a-z]))|\s(?=\*{1,6}[A-Z])|"
                              r"\s(?=(?:[a-h]|[j-z]|[ivx]{1,4}|\d{1,2})[.)]\s+[A-Z0-9])")
_SENT_SPLIT_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”')]))\s+(?=[A-Z0-9\"“(\[•▪])")
_BULLET_HEAD_RE = re.compile(r"^([•▪◦●]|[–-](?=\s)|o(?=\s[A-Z])|\*{1,6}(?=[A-Z])|(?:[ivx]{1,4}|[a-h]|[j-z]|\d{1,2})[.)](?=\s))\s*")
_LIST_HEADER_RE = re.compile(r"(?:conditions?|indications?|diagnos[ie]s|criteria|requirements?|situations?|modifiers?)"
                             r"(?:\(s\))?\s*:?\s*$", re.I)


def _marker_kind(m: str) -> str:
    """Sibling class of a list marker: 'a.'/'b.' -> alpha, 'i.'/'ii.' -> roman, '1.' -> num."""
    m = m.rstrip(".)")
    if re.fullmatch(r"[ivx]{1,4}", m):
        return "roman"
    if re.fullmatch(r"[a-z]", m):
        return "alpha"
    if m.isdigit():
        return "num"
    return m
CONT_RE = re.compile(
    r"^(?:this|these|that|those|such|the other|the latter|the former|no modifiers?|instead|however|in addition|"
    r"additionally|also|for example|for instance|e\.g\.|i\.e\.|example|otherwise|in this case|in these cases|"
    r"in such cases|in that case|then|therefore|thus|as a result|accordingly|similarly|likewise|it|they|both|"
    r"neither|either|note\b|if so|when this|except|unless|but|and|or|nor|rather|consequently)\b",
    re.I,
)


_CODE_LIST_HEAD_RE = re.compile(r"^(?:(?:non-?reimbursable |reimbursable )?(?:procedure|cpt|hcpcs|cpt/hcpcs)\s+codes?(?:\(s\))?|"
                                r"codes?(?:\(s\))?\s*:)", re.I)


def _is_code_list_line(txt: str) -> bool:
    """'Procedure Code(s) 83516', 'CPT codes: 86003, 86008' - the code list that belongs to the
    statement before it."""
    if _CODE_LIST_HEAD_RE.match(txt):
        return True
    toks = re.findall(r"\S+", txt)
    return bool(toks) and len(extract_codes(txt)) >= max(2, 0.6 * len(toks))


def split_pieces(text: str) -> List[Tuple[str, Optional[str]]]:
    """Split into (sentence, bullet_marker). marker: '•','▪','–','o' for a new bullet item,
    'cont' for a further sentence of the same bullet item, None for plain prose."""
    text = clean_text(text)
    if not text:
        return []
    prot = _ABBR_RE.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    pieces: List[Tuple[str, Optional[str]]] = []
    for chunk in _BULLET_SPLIT_RE.split(prot):
        chunk = chunk.strip()
        if not chunk:
            continue
        mk = None
        m = _BULLET_HEAD_RE.match(chunk)
        if m:
            mk = _marker_kind({"-": "–"}.get(m.group(1), m.group(1)))
        sents = [s.strip() for s in _SENT_SPLIT_RE.split(chunk) if s.strip()]
        # never split a list marker ("a.", "ii.") off its own item
        if len(sents) > 1 and _BULLET_HEAD_RE.fullmatch(sents[0] + " "):
            sents = [sents[0] + " " + sents[1]] + sents[2:]
        for i, s in enumerate(sents):
            s = s.replace("\x00", ".")
            if re.fullmatch(r"[•▪◦●–\-o]?\s*", s):
                continue
            pieces.append((s, mk if i == 0 else ("cont" if mk else None)))
    return pieces


def build_groups(pieces: List[Tuple[str, Optional[str]]], with_heads: bool = False):
    """Group pieces into complete rule statements.
    - a lead-in ending in ':' keeps its bullet list
    - dependent sentences ('This...', 'No modifiers will...', 'Instead...') stay with the previous one
    - same-level bullets that are NOT under a lead-in are separate statements
    """
    groups: List[Dict[str, Any]] = []
    for txt, mk in pieces:
        if groups:
            g = groups[-1]
            prev = g["items"][-1].rstrip()
            attach = False
            if mk == "cont":
                attach = True
            elif mk:
                if (g["list_open"] and mk != g["head"]) or prev.endswith(":"):
                    attach = True
            else:
                if prev.endswith((":", ";", ",")) or CONT_RE.match(txt) or txt[:1].islower() or _is_code_list_line(txt):
                    attach = True
                else:
                    g["list_open"] = False
            if attach:
                g["items"].append(txt)
                g["marks"].append(mk)
                if txt.rstrip().endswith(":") or _LIST_HEADER_RE.search(txt):
                    g["list_open"] = True
                continue
        groups.append({"items": [txt], "marks": [mk], "head": mk if mk != "cont" else None,
                       "list_open": txt.rstrip().endswith(":") or bool(_LIST_HEADER_RE.search(txt))})
    if with_heads == "marks":
        return [(g["items"], g["head"], g["marks"]) for g in groups]
    if with_heads:
        return [(g["items"], g["head"]) for g in groups]
    return [g["items"] for g in groups]


def _is_heading_fragment(s: str) -> bool:
    """Orphan section headings left in the text ('Procedures and materials Findings')."""
    s = s.strip()
    if not s or s[-1] in ".:;?!)\"'" or extract_codes(s):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'/-]*", s)
    if not words or len(words) > 14:
        return False
    caps = sum(1 for w in words if w[0].isupper() or w.lower() in ("and", "or", "of", "the", "with", "for", "in", "to", "a", "an"))
    return caps / len(words) >= 0.85 and not has_directive(s)


POINTER_RE = re.compile(r"^(?:please )?(?:refer to|see|for (?:more|additional) information)\b", re.I)


def _group_is_rule(items: List[str]) -> bool:
    return has_directive(" ".join(i for i in items if not POINTER_RE.match(i)))


_AND_LEAD_RE = re.compile(
    r"\ball (?:of )?the following\b|\beach of the following\b|\bboth of the following\b|"
    r"\bmust (?:meet|include|satisfy|contain|be met|be satisfied|be documented)\b|"
    r"\b(?:criteria|requirements?) (?:must|are required|have to)\b|\bthe following (?:criteria|requirements) must\b",
    re.I)
_OR_LEAD_RE = re.compile(r"\b(?:any|one or more|at least one|either|one) of the following\b|\bany of the\b", re.I)


def _list_is_conjunctive(lead_text: str, spans: List[List[str]]) -> bool:
    """True when the listed conditions only apply TOGETHER (must not be split):
    'all of the following', 'must meet the following criteria', items joined by '; and'."""
    if _OR_LEAD_RE.search(lead_text):
        return False
    if any(re.search(r"(?:;|,)?\s+and\s*$", " ".join(sp).strip()) for sp in spans[:-1]):
        return True
    if any(re.search(r"(?:;|,)?\s+or\s*$", " ".join(sp).strip()) for sp in spans[:-1]):
        return False
    return bool(_AND_LEAD_RE.search(lead_text))


class _Node:
    __slots__ = ("texts", "kind", "children")

    def __init__(self, texts, kind):
        self.texts, self.kind, self.children = list(texts), kind, []

    @property
    def text(self) -> str:
        return " ".join(self.texts)

    def flat(self) -> List[str]:
        out = list(self.texts)
        for c in self.children:
            out.extend(c.flat())
        return out


def _build_list_tree(lead: List[str], items: List[str], marks: List[Optional[str]]) -> _Node:
    """Rebuild list nesting from a flattened PDF list.
    - an item right after one that ends with ':' is its child (deeper level)
    - a marker already open on the stack returns to that level (sibling)
    - when the same marker is open at several levels, a header-like item (ending ':') goes to the
      shallowest header level, anything else to the deepest"""
    root = _Node(lead, None)
    stack: List[_Node] = [root]
    last = root
    for txt, mk in zip(items, marks):
        if mk in (None, "cont"):
            last.texts.append(txt)
            continue
        node = _Node([txt], mk)
        if last is root or last.text.rstrip().endswith(":"):
            parent_idx = stack.index(last)
        else:
            levels = [i for i in range(1, len(stack)) if stack[i].kind == mk]
            if not levels:
                parent_idx = stack.index(last)
            elif len(levels) == 1:
                parent_idx = levels[0] - 1
            else:
                hdr = [i for i in levels if stack[i].text.rstrip().endswith(":")]
                parent_idx = (hdr[0] if txt.rstrip().endswith(":") and hdr else levels[-1]) - 1
        del stack[parent_idx + 1:]
        stack[parent_idx].children.append(node)
        stack.append(node)
        last = node
    return root


def _children_are_alternatives(node: _Node, top_level: bool) -> bool:
    kids = node.children
    if len(kids) < 2:
        return False
    if _list_is_conjunctive(node.text, [k.flat() for k in kids]):
        return False
    if top_level:
        return True
    if _OR_LEAD_RE.search(node.text):
        return True
    # a sub-list of short condition names ("every 90 days for: ▪ Multiple myeloma ▪ Uveal melanoma")
    # is a list of alternatives; a sub-list of statements (definition + payment reduction) is ONE rule
    return all(len(re.findall(r"\w+", k.text)) <= 14 and not has_directive(k.text) and not k.children
               or k.text.rstrip().endswith(":") for k in kids)


def _expand(node: _Node, prefix: List[str], top_level: bool) -> List[List[str]]:
    here = prefix + node.texts
    if not node.children:
        return [here]
    if not _children_are_alternatives(node, top_level):
        return [prefix + node.flat()]
    out = []
    for k in node.children:
        out.extend(_expand(k, here, False))
    return out


def _split_long_list(items: List[str], head, marks: List[Optional[str]], chunk_max: int, depth: int = 0):
    """One rule per listed condition: lead-in + ONE alternative (with its own sub-items) per rule.
    Lists whose items must all apply together stay one rule (see _list_is_conjunctive)."""
    lead_end = next((i for i, t in enumerate(items) if t.rstrip().endswith(":") or _LIST_HEADER_RE.search(t)), None)
    if lead_end is None:
        return [(items, head)]
    first_item = next((i for i in range(lead_end + 1, len(items)) if marks[i] and marks[i] != "cont"), None)
    if first_item is None:
        return [(items, head)]
    # a second lead-in after the first list ("Any time for any of the following scenarios is
    # excluded ...:") starts a NEW list with its own lead-in
    nxt = next((j for j in range(first_item + 1, len(items))
                if marks[j] in (None, "cont") and items[j].rstrip().endswith(":")), None)
    if nxt is not None and depth < 5:
        a = _split_long_list(items[:nxt], head, marks[:nxt], chunk_max, depth + 1)
        b = _split_long_list(items[nxt:], None, [None] + marks[nxt + 1:], chunk_max, depth + 1)
        if len(a) == 1 and len(b) == 1:
            return [(items, head)]
        return [(x, "split") for x, _ in a] + [(x, "split") for x, _ in b]
    root = _build_list_tree(items[:first_item], items[first_item:], marks[first_item:])
    parts = _expand(root, [], True)
    if len(parts) < 2:
        return [(items, head)]
    # only split a list whose lead-in states the rule (or whose every item does); a list under a
    # definition ("CMS defines ALS2 as ... including (1) ... (2) ...") stays together
    if not has_directive(root.text) and not all(has_directive(" ".join(p[len(root.texts):])) for p in parts):
        return [(items, head)]
    return [(p, "split") for p in parts]


def select_rule_chunks(
    text: str,
    boiler: Optional[CorpusBoilerplate] = None,
    claimed: Optional[Set[str]] = None,
    whole_max: int = 2000,
    chunk_max: int = 3000,
    headings: Optional[List[str]] = None,
    skip_claimed: bool = True,
) -> List[str]:
    """Return the complete, verbatim rule statements in a block of policy text."""
    pieces = split_pieces(text)
    keep = []
    long_claimed = [c for c in claimed if len(c) >= 30] if claimed else []
    for s, mk in pieces:
        k = norm_key(s)
        # only real sentences can be "already used" - never short fragments like "a." or "Condition(s)"
        if skip_claimed and claimed is not None and len(k) >= 15 and (
                k in claimed or any(c in k for c in long_claimed if len(c) >= 0.5 * len(k))):
            continue
        if (boiler.is_boilerplate(s) if boiler else BOILERPLATE_RE.search(s)):
            continue
        if not mk and _is_heading_fragment(s):
            continue
        if headings:
            rest = s
            for h in headings:
                rest = rest.replace(h, " ")
            if len(re.sub(r"[^A-Za-z0-9]", "", rest)) <= 3:
                continue  # piece is only section headings run together
        keep.append((s, mk))
    if not keep:
        return []
    gh, inherit = [], []
    for items, head, marks in build_groups(keep, with_heads="marks"):
        parts = _split_long_list(items, head, marks, chunk_max)
        whole_is_rule = _group_is_rule(items)
        gh.extend(parts)
        # a part split out of a rule-bearing list is a rule even if its own lead-in has no keyword
        inherit.extend([whole_is_rule and len(parts) > 1] * len(parts))
    groups = [g for g, _ in gh]
    heads = [h for _, h in gh]
    flags = [_group_is_rule(g) or inh for g, inh in zip(groups, inherit)]
    if not any(flags):
        return []
    if claimed is not None:
        for s, _ in keep:
            claimed.add(norm_key(s))
    total = sum(len(" ".join(g)) for g in groups)
    has_bullet_rules = sum(1 for h, f in zip(heads, flags) if h and f) >= 2 or "split" in heads
    if total <= whole_max and not has_bullet_rules:
        # short section: the whole section is one complete rule. Trim leading /
        # trailing groups that carry no rule keyword (pointers, background).
        while groups and not flags[-1]:
            groups.pop(); flags.pop(); heads.pop()
        while groups and not flags[0] and not " ".join(groups[0]).rstrip().endswith(":"):
            groups.pop(0); flags.pop(0); heads.pop(0)
        return [" ".join(" ".join(g) for g in groups)]
    # long section, or a section made of separate bullet rules:
    #  - each top-level bullet statement stays its own rule (distinct codes / outcomes)
    #  - prose is clubbed into contiguous verbatim runs (connecting sentences between
    #    rule statements are kept so the text reads exactly as in the policy), up to chunk_max
    chunks: List[str] = []
    cur: List[Tuple[str, bool]] = []

    def flush(trim: bool = True):
        while trim and cur and not cur[-1][1]:
            cur.pop()
        if cur:
            chunks.append(" ".join(t for t, _ in cur))
        cur.clear()

    for g, h, f in zip(groups, heads, flags):
        gtxt = " ".join(g)
        if h and f:
            flush(trim=False)   # prose right before a list stays with the preceding rule
            chunks.append(gtxt)
            continue
        if not cur and not f:
            continue  # leading background text before the first rule statement
        if cur and len(" ".join(t for t, _ in cur)) + len(gtxt) > chunk_max:
            flush()
            if not f:
                continue
        cur.append((gtxt, f))
    flush()
    return chunks


def first_clause(text: str, rx: re.Pattern) -> str:
    m = rx.search(text)
    return m.group(0).strip() if m else ""


TRIGGER_RE = re.compile(r"\b(?:only when|only if|if|when|provided that|in the following situations?|for individuals)\b[^.;]{3,220}", re.I)
EXCEPTION_RE = re.compile(r"\b(?:unless|except(?:ion)?s?(?: when| for| in| that| those)?|does not apply|do not apply|"
                          r"will not apply|in lieu of|override)\b[^.;]{3,220}", re.I)


# ==============================================================================
# 6. RULE MODEL
# ==============================================================================

@dataclass
class Rule:
    payer: str
    policy_key: str
    policy_title: str
    policy_number: str
    line_of_business: str
    billing_class: str
    effective_date: str
    source_section: str
    source_type: str           # prose | bullet | qa | table | attachment_only | table_only
    text: str                  # VERBATIM policy text
    order: float = 0.0
    source_pdf: str = ""
    source_page: Any = ""
    rule_id: str = ""
    category: str = ""
    secondary: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    codes: List[str] = field(default_factory=list)
    code_sources: List[str] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)
    attachment_values: List[str] = field(default_factory=list)
    code_params: List[Dict[str, str]] = field(default_factory=list)
    modifiers: List[str] = field(default_factory=list)
    mods_required: List[str] = field(default_factory=list)
    mods_prohibited: List[str] = field(default_factory=list)
    bill_types: List[str] = field(default_factory=list)
    merged_sections: List[str] = field(default_factory=list)
    forced_category: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def add_codes(self, codes: Iterable[str], source: str) -> None:
        have = set(self.codes)
        new = [c for c in codes if c not in have]
        if new:
            self.codes.extend(new)
            if source and source not in self.code_sources:
                self.code_sources.append(source)

    def add_modifiers(self, mods: Iterable[str]) -> None:
        for m in mods:
            if m not in self.modifiers:
                self.modifiers.append(m)


def club_rules(rules: List[Rule]) -> List[Rule]:
    """Within ONE policy: state each rule once.
    (a) identical text after masking codes  -> merge, union codes
    (b) text fully contained in another rule -> merge into the larger rule
    """
    def sig(t: str) -> str:
        t = RANGE_RE.sub(" # ", t)
        t = CODE_RE.sub("#", t)
        return re.sub(r"(?:#\W*)+", "# ", norm_key(t))

    out: List[Rule] = []
    by_sig: Dict[str, Rule] = {}
    for r in rules:
        s = sig(r.text)
        if s in by_sig:
            keep = by_sig[s]
            keep.add_codes(r.codes, "clubbed duplicate statement")
            keep.add_modifiers(r.modifiers)
            keep.code_params.extend(r.code_params)
            if r.source_section not in keep.merged_sections and r.source_section != keep.source_section:
                keep.merged_sections.append(r.source_section)
            continue
        by_sig[s] = r
        out.append(r)
    # containment
    keys = [(r, norm_key(r.text)) for r in out]
    dropped = set()
    for i, (r, k) in enumerate(keys):
        if len(k) < 60:
            continue
        for j, (r2, k2) in enumerate(keys):
            if i == j or j in dropped or len(k2) <= len(k):
                continue
            if r.source_type == r2.source_type and k in k2:
                r2.add_codes(r.codes, "clubbed duplicate statement")
                r2.add_modifiers(r.modifiers)
                r2.code_params.extend(r.code_params)
                dropped.add(i)
                break
    return [r for i, r in enumerate(out) if i not in dropped]


# ==============================================================================
# 7. DERIVED ENGINEERING FIELDS (never mixed into the verbatim rule text)
# ==============================================================================

AUDIT_ACTION = {
    "TIME_SPAN_LIMIT": "DENY_EXCESS_FREQUENCY",
    "ONCE_IN_A_LIFETIME": "DENY_EXCESS_FREQUENCY",
    "ADD_ON_PRIMARY": "DENY_LINE_NO_PRIMARY",
    "BUNDLING_INCLUSION": "DENY_LINE_BUNDLED",
    "FREQUENCY_LIMIT": "DENY_EXCESS_UNITS",
    "AGE_GENDER_RESTRICTION": "DENY_LINE_AGE_GENDER",
    "PLACE_OF_SERVICE": "DENY_LINE_POS",
    "BILL_TYPE_REVENUE_CODE": "DENY_LINE_BILL_TYPE",
    "DIAGNOSIS_REQUIREMENT": "DENY_LINE_DIAGNOSIS",
    "PRIOR_AUTHORIZATION": "DENY_NO_AUTH",
    "PAYMENT_ADJUSTMENT": "ADJUST_REIMBURSEMENT",
    "MODIFIER_RULE": "DENY_OR_ADJUST_MODIFIER",
    "DENIAL_PROHIBITION": "DENY_LINE",
    "REIMBURSEMENT_CRITERIA": "PAY_IF_CRITERIA_MET",
    "EXCEPTION_OVERRIDE": "BYPASS_EDIT_IF_EXCEPTION",
    "POLICY_REQUIREMENT": "FLAG_SUSPEND_FOR_REVIEW",
}


def incorrect_billing_condition(r: Rule) -> str:
    cat = r.category
    t = r.text.lower()
    att = f" per {', '.join(r.attachments)}" if r.attachments else ""
    params = " (per-code values in Attachment_Values / Rule_Code_Map)" if r.code_params else ""
    mods = ", ".join(r.modifiers)
    if cat == "TIME_SPAN_LIMIT":
        return ("Same code billed more than once (or > 1 unit) within its designated time span" + params +
                " for the same member by the Same Group Physician/QHP.")
    if cat == "ONCE_IN_A_LIFETIME":
        return ("Procedure billed more than once in the member's lifetime for any code in the same Code Family"
                + params + ", by the same or a different provider.")
    if cat == "ADD_ON_PRIMARY":
        return ("Add-on code billed without an eligible primary code" + att +
                " for the same member and date of service, or billed when the primary code is not reimbursed.")
    if cat == "BUNDLING_INCLUSION":
        if "comprehensive" in t and "component" in t:
            return ("Component code billed with its comprehensive code" + att +
                    " for the same member / provider group within the same date of service or time span.")
        return "Bundled / incidental code billed separately with the primary or comprehensive service on the same date of service."
    if cat == "FREQUENCY_LIMIT":
        return "Billed units or frequency exceed the limit stated in the rule" + params + "."
    if cat == "AGE_GENDER_RESTRICTION":
        return "Member age (or gender) on the date of service is outside the range allowed for the code" + params + "."
    if cat == "PLACE_OF_SERVICE":
        return "Service billed with a place of service / setting not allowed by the rule."
    if cat == "BILL_TYPE_REVENUE_CODE":
        bt = f" ({', '.join(r.bill_types)})" if r.bill_types else ""
        return f"Claim type of bill / revenue code{bt} does not satisfy the rule."
    if cat == "DIAGNOSIS_REQUIREMENT":
        return "Service billed without a diagnosis that satisfies the rule (or with an excluded diagnosis)."
    if cat == "PRIOR_AUTHORIZATION":
        return "Service billed without the required authorization / referral."
    if cat == "PAYMENT_ADJUSTMENT":
        return "Line paid above the adjusted amount the rule requires (percentage / reduction not applied)."
    if cat == "MODIFIER_RULE":
        if r.mods_prohibited and not r.mods_required:
            return f"Line billed with modifier(s) {', '.join(r.mods_prohibited)} that the rule does not allow / recognize."
        if r.mods_required:
            return f"Line billed without the required modifier(s) {', '.join(r.mods_required)}" + att + "."
        return f"Line billed with an inappropriate or missing modifier{(' (' + mods + ')') if mods else ''}" + att + "."
    if cat == "DENIAL_PROHIBITION":
        return "Line billed for a service the rule states is not reimbursable / must not be reported."
    if cat == "REIMBURSEMENT_CRITERIA":
        return "Line paid although the reimbursement criteria in the rule are not met."
    if cat == "EXCEPTION_OVERRIDE":
        return "Exception to a base edit: line meeting the exception must NOT be denied by the base edit."
    return "Review against the policy rule text."


def summarize_params(code_params: List[Dict[str, str]], max_values: int = 15, max_codes: int = 10) -> str:
    """Compact, readable summary of per-code parameters for the Rules sheet."""
    if not code_params:
        return ""
    by_name: "OrderedDict[str, OrderedDict[str, List[str]]]" = OrderedDict()
    rel_counter: Counter = Counter()
    rel_codes: Dict[str, Set[str]] = defaultdict(set)
    for p in code_params:
        if p.get("related_code"):
            rel_counter[p.get("relationship", "RELATED")] += 1
            rel_codes[p.get("relationship", "RELATED")].add(p["code"])
        if p.get("parameter_name"):
            by_name.setdefault(p["parameter_name"], OrderedDict()).setdefault(p.get("parameter_value", ""), []).append(p["code"])
    parts = []
    for rel, n in rel_counter.items():
        parts.append(f"{rel}: {len(rel_codes[rel])} codes, {n} code pairs (full pairs in Rule_Code_Map)")
    for name, vals in by_name.items():
        if re.search(r"date", name, re.I):
            parts.append(f"{name}: per code in Rule_Code_Map")
            continue
        if len(vals) > max_values:
            parts.append(f"{name}: {len(vals)} distinct values (per code in Rule_Code_Map)")
            continue
        items = []
        for v, cs in sorted(vals.items(), key=lambda kv: -len(kv[1])):
            cs = sorted(set(cs), key=code_sort_key)
            show = compress_codes(cs[:max_codes]) + (f", … (+{len(cs) - max_codes})" if len(cs) > max_codes else "")
            items.append(f"{v} [{len(cs)}]: {show}")
        parts.append(f"{name} → " + " | ".join(items))
    return " || ".join(parts)


def finalize(r: Rule) -> None:
    cat, sec, kws = classify(r.text)
    if r.forced_category:
        if cat != r.forced_category and cat not in sec:
            sec = [cat] + sec
        cat = r.forced_category
        sec = [c for c in sec if c != cat]
    r.category, r.secondary, r.keywords = cat, sec, kws
    r.add_modifiers(extract_modifiers(r.text))
    req, pro = classify_modifiers(r.text, r.modifiers)
    r.mods_required = sorted(set(r.mods_required) | set(req))
    r.mods_prohibited = sorted(set(r.mods_prohibited) | set(pro))
    r.mods_required = [m for m in r.mods_required if m not in r.mods_prohibited]
    r.bill_types = extract_bill_types(r.text)


# ==============================================================================
# 8. OUTPUT
# ==============================================================================

RULE_COLUMNS = [
    "Rule_ID", "Payer", "Policy_Title", "Policy_Number", "Line_of_Business", "Billing_Class", "Effective_Date",
    "Source_Section", "Source_Type", "Policy_Rule_Text", "Rule_Category", "Secondary_Categories", "Trigger_Keywords",
    "Target_Codes", "Possible_icd_codes", "Code_Count", "Code_Source", "Attachment_Reference", "Attachment_Values",
    "Target_Modifiers", "Modifiers_Required", "Modifiers_Prohibited", "Bill_Types",
    "Trigger_Condition", "Exception_Text", "Audit_Action", "Incorrect_Billing_Condition",
    "Required_Claim_Fields", "SQL_Query", "Source_PDF", "Source_Page", "Review_Flag",
]
CODE_MAP_COLUMNS = ["Rule_ID", "Policy_Title", "Line_of_Business", "Rule_Category", "Code",
                    "Related_Code", "Relationship", "Parameter_Name", "Parameter_Value", "Code_Source"]
XL_MAX = 32000


def rule_record(r: Rule, lob_sql_value: str = "", claim_cols: Optional[List[str]] = None) -> Dict[str, Any]:
    codes = sorted(r.codes, key=code_sort_key)
    action = AUDIT_ACTION.get(r.category, "")
    q = sql_skeleton.build_query(r, action)
    icds = q.get("icd_codes", [])
    possible_icd_str = compress_codes(icds) if icds else ""
    flag = review_flag(r)
    if q["todo"]:
        flag = "; ".join(x for x in (flag, "SQL needs input: " + ", ".join(q["todo"])) if x)
    return {
        "Rule_ID": r.rule_id,
        "Payer": r.payer,
        "Policy_Title": r.policy_title,
        "Policy_Number": r.policy_number,
        "Line_of_Business": r.line_of_business,
        "Billing_Class": r.billing_class,
        "Effective_Date": r.effective_date,
        "Source_Section": " | ".join([r.source_section] + r.merged_sections),
        "Source_Type": r.source_type,
        "Policy_Rule_Text": r.text,
        "Rule_Category": r.category,
        "Secondary_Categories": ", ".join(r.secondary),
        "Trigger_Keywords": ", ".join(r.keywords),
        "Target_Codes": compress_codes(codes),
        "Possible_icd_codes": possible_icd_str,
        "Code_Count": len(codes),
        "Code_Source": "; ".join(r.code_sources),
        "Attachment_Reference": ", ".join(r.attachments),
        "Attachment_Values": " || ".join(v for v in r.attachment_values if v),
        "Target_Modifiers": ", ".join(r.modifiers),
        "Modifiers_Required": ", ".join(r.mods_required),
        "Modifiers_Prohibited": ", ".join(r.mods_prohibited),
        "Bill_Types": ", ".join(r.bill_types),
        "Trigger_Condition": first_clause(r.text, TRIGGER_RE),
        "Exception_Text": first_clause(r.text, EXCEPTION_RE),
        "Audit_Action": action,
        "Incorrect_Billing_Condition": incorrect_billing_condition(r),
        # exactly the columns the rule's SQL reads, so the two always match
        "Required_Claim_Fields": ", ".join(q["columns"]),
        "SQL_Query": q["sql"],
        "Source_PDF": r.source_pdf,
        "Source_Page": r.source_page if r.source_page is not None else "",
        "Review_Flag": flag,
        "_codes_full": codes,
    }


def review_flag(r: Rule) -> str:
    flags = []
    if r.text[:1].islower():
        flags.append("Starts mid-sentence (text lost at a PDF page/table break) - verify against source PDF")
    if r.source_type in ("table_only", "attachment_only"):
        flags.append("No prose rule found for this table/attachment - rule text is the table/attachment reference")
    if len(r.text) > 3200:
        flags.append("Long rule - review whether it should be split")
    if r.text.rstrip().endswith(":") and not re.search(r"example below:\s*$", r.text):
        flags.append("Ends with a lead-in ':' - the list it introduces was not found in the extracted source")
    if r.category == "POLICY_REQUIREMENT":
        flags.append("No category keyword matched")
    return "; ".join(flags)


def code_map_rows(rules: List[Rule]) -> List[Dict[str, Any]]:
    rows = []
    for r in rules:
        seen_codes = set()
        for p in r.code_params:
            rows.append({
                "Rule_ID": r.rule_id, "Policy_Title": r.policy_title, "Line_of_Business": r.line_of_business,
                "Rule_Category": r.category, "Code": p.get("code", ""), "Related_Code": p.get("related_code", ""),
                "Relationship": p.get("relationship", ""), "Parameter_Name": p.get("parameter_name", ""),
                "Parameter_Value": p.get("parameter_value", ""), "Code_Source": p.get("source", ""),
            })
            seen_codes.add(p.get("code", ""))
        for c in sorted(r.codes, key=code_sort_key):
            if c not in seen_codes:
                rows.append({
                    "Rule_ID": r.rule_id, "Policy_Title": r.policy_title, "Line_of_Business": r.line_of_business,
                    "Rule_Category": r.category, "Code": c, "Related_Code": "", "Relationship": "",
                    "Parameter_Name": "", "Parameter_Value": "", "Code_Source": "; ".join(r.code_sources),
                })
    return rows


def summary_rows(rules: List[Rule], extra: List[Tuple[str, str, Any]]) -> List[Dict[str, Any]]:
    rows = [{"Section": s, "Metric": m, "Value": v} for s, m, v in extra]
    rows.append({"Section": "Rules", "Metric": "Total rules", "Value": len(rules)})
    rows.append({"Section": "Rules", "Metric": "Policies with at least one rule", "Value": len({r.policy_key for r in rules})})
    rows.append({"Section": "Rules", "Metric": "Rules carrying attachment / table code lists",
                 "Value": sum(1 for r in rules if r.attachments)})
    rows.append({"Section": "Rules", "Metric": "Distinct codes linked to rules",
                 "Value": len({c for r in rules for c in r.codes})})
    for k, v in Counter(r.category for r in rules).most_common():
        rows.append({"Section": "Rule Category", "Metric": k, "Value": v})
    for k, v in Counter(r.source_type for r in rules).most_common():
        rows.append({"Section": "Source Type", "Metric": k, "Value": v})
    for k, v in Counter(r.line_of_business for r in rules).most_common():
        rows.append({"Section": "Line of Business", "Metric": k, "Value": v})
    for k, v in Counter(r.policy_title for r in rules).most_common():
        rows.append({"Section": "Rules per Policy", "Metric": k, "Value": v})
    return rows


def _xl(v: Any) -> Any:
    if isinstance(v, str) and len(v) > XL_MAX:
        return v[:XL_MAX] + " …[truncated in Excel; full value in JSON/CSV]"
    if isinstance(v, (list, dict)):
        return json.dumps(v)[:XL_MAX]
    return v


def write_workbook(path: Path, sheets: List[Tuple[str, List[str], List[Dict[str, Any]], Dict[str, int], Set[str]]]) -> None:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    hfill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    hfont = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    wrap = Alignment(vertical="top", wrap_text=True)
    top = Alignment(vertical="top")
    for name, cols, rows, widths, wrap_cols in sheets:
        ws = wb.create_sheet(title=name[:31])
        ws.freeze_panes = "A2"
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 18)
        hdr = []
        for c in cols:
            cell = WriteOnlyCell(ws, value=c)
            cell.fill, cell.font = hfill, hfont
            hdr.append(cell)
        ws.append(hdr)
        styled = len(rows) <= 20000
        for row in rows:
            vals = []
            for c in cols:
                v = _xl(row.get(c, ""))
                if styled:
                    cell = WriteOnlyCell(ws, value=v)
                    cell.alignment = wrap if c in wrap_cols else top
                    vals.append(cell)
                else:
                    vals.append(v)
            ws.append(vals)
    wb.save(path)


RULE_WIDTHS = {
    "Rule_ID": 16, "Payer": 9, "Policy_Title": 34, "Policy_Number": 14, "Line_of_Business": 16, "Billing_Class": 12,
    "Source_Section": 30, "Source_Type": 12, "Policy_Rule_Text": 80, "Rule_Category": 24, "Secondary_Categories": 26,
    "Trigger_Keywords": 26, "Target_Codes": 34, "Possible_icd_codes": 26, "Code_Count": 9, "Code_Source": 24, "Attachment_Reference": 28,
    "Attachment_Values": 45, "Incorrect_Billing_Condition": 50, "Required_Claim_Fields": 30,
    "SQL_Query": 90, "Trigger_Condition": 34, "Exception_Text": 34,
}
RULE_WRAP = {"Policy_Rule_Text", "Target_Codes", "Attachment_Values", "Incorrect_Billing_Condition",
             "SQL_Query", "Source_Section", "Trigger_Condition", "Exception_Text", "Policy_Title"}


MAIN_COLUMNS = list(RULE_COLUMNS)


def _uniq(seq: Iterable[str]) -> List[str]:
    return [x for x in OrderedDict.fromkeys(x for x in seq if x)]


def main_rows(rules: List[Rule], records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Main Rules sheet.
    - Default: one row per rule, carrying all its codes (unchanged).
    - When a CPT code has 2+ rules in the SAME policy, that code's rules are clubbed into one row.
      Codes with exactly the same set of rules share that clubbed row.
    - A clubbed code is removed from the individual rule rows (each code appears once per policy);
      a rule whose codes were all clubbed is not repeated as its own row.
    Codes a rule received only as a policy-scope fallback do not trigger clubbing."""
    by_policy: "OrderedDict[str, List[Tuple[Rule, Dict[str, Any]]]]" = OrderedDict()
    for r, rec in zip(rules, records):
        by_policy.setdefault(r.policy_key, []).append((r, rec))
    out: List[Dict[str, Any]] = []
    for pkey, items in by_policy.items():
        def own(r: Rule) -> List[str]:
            if r.code_sources and all(src.startswith("policy scope") for src in r.code_sources):
                return []
            return r.codes
        code_rules: "OrderedDict[str, List[int]]" = OrderedDict()
        for i, (r, _) in enumerate(items):
            for c in own(r):
                code_rules.setdefault(c, []).append(i)
        multi = {c: tuple(ix) for c, ix in code_rules.items() if len(ix) > 1}
        groups: "OrderedDict[Tuple[int, ...], List[str]]" = OrderedDict()
        for c, ix in multi.items():
            groups.setdefault(ix, []).append(c)
        emitted_groups = set()
        stem = items[0][0].rule_id.rsplit("-R", 1)[0] if "-R" in items[0][0].rule_id else items[0][0].rule_id
        gseq = 0

        def clubbed_row(ix: Tuple[int, ...], codes: List[str]) -> Dict[str, Any]:
            nonlocal gseq
            gseq += 1
            grp = [items[i] for i in ix]
            codes = sorted(codes, key=code_sort_key)
            cs = set(codes)
            first = dict(grp[0][1])
            def u(col, sep=", "):
                return sep.join(_uniq(x for _, rec in grp for x in str(rec[col]).split(sep)))
            conds = _uniq(rec["Incorrect_Billing_Condition"] for _, rec in grp)
            params = [p for r, _ in grp for p in r.code_params if p.get("code") in cs or p.get("related_code") in cs]
            row = {k: first.get(k, "") for k in RULE_COLUMNS}
            clubbed_queries = []
            cur_len = 0
            for _, rec in grp:
                q = rec.get("SQL_Query", "")
                if not q:
                    continue
                if cur_len + len(q) + 2 > 28000:
                    remaining = len(grp) - len(clubbed_queries)
                    clubbed_queries.append(f"-- ({remaining} more queries clubbed in this group; see Rule_Detail sheet for individual queries)")
                    break
                clubbed_queries.append(q)
                cur_len += len(q) + 2
            row.update({
                "Rule_ID": f"{stem}-G{gseq:02d}",
                "_clubbed": True,
                "Source_Section": u("Source_Section", " | "),
                "Source_Type": u("Source_Type"),
                "Policy_Rule_Text": "\n\n".join(f"{n}) [{r.rule_id}] {r.text}" for n, (r, _) in enumerate(grp, 1)),
                "Rule_Category": u("Rule_Category"),
                "Secondary_Categories": u("Secondary_Categories"),
                "Trigger_Keywords": u("Trigger_Keywords"),
                "Target_Codes": compress_codes(codes),
                "Possible_icd_codes": u("Possible_icd_codes", ", "),
                "Code_Count": len(codes),
                "Code_Source": u("Code_Source", "; "),
                "Attachment_Reference": u("Attachment_Reference"),
                "Attachment_Values": summarize_params(params) if params else "",
                "Target_Modifiers": u("Target_Modifiers"), "Modifiers_Required": u("Modifiers_Required"),
                "Modifiers_Prohibited": u("Modifiers_Prohibited"), "Bill_Types": u("Bill_Types"),
                "Trigger_Condition": u("Trigger_Condition", " | "), "Exception_Text": u("Exception_Text", " | "),
                "Audit_Action": u("Audit_Action"),
                "Incorrect_Billing_Condition": "\n".join(f"{n}) {c}" for n, c in enumerate(conds, 1))
                if len(conds) > 1 else (conds[0] if conds else ""),
                "Required_Claim_Fields": u("Required_Claim_Fields"),
                "SQL_Query": "\n\n".join(clubbed_queries),
                "Source_Page": u("Source_Page"),
                "Review_Flag": u("Review_Flag", "; "),
                "_codes_full": codes,
            })
            return row

        for i, (r, rec) in enumerate(items):
            # clubbed rows are placed where their first rule appears
            for ix, codes in groups.items():
                if ix[0] == i and ix not in emitted_groups:
                    emitted_groups.add(ix)
                    out.append(clubbed_row(ix, codes))
            own_codes = own(r)
            if own_codes:
                rest = [c for c in r.codes if c not in multi]
                if not rest:
                    continue  # every code of this rule is in a clubbed row
            else:
                rest = list(r.codes)
            row = {k: rec.get(k, "") for k in RULE_COLUMNS}
            row.update({"_codes_full": rest})
            if len(rest) != len(r.codes):
                rs = set(rest)
                row["Target_Codes"] = compress_codes(rest)
                row["Code_Count"] = len(rest)
                params = [p for p in r.code_params if p.get("code") in rs]
                if r.attachments:
                    row["Attachment_Values"] = summarize_params(params) if params else f"{len(rest)} codes"
            out.append(row)
    return out


def export_all(rules: List[Rule], records: List[Dict[str, Any]], out_dir: Path, extra_summary, extra_sheets=None,
               mirror_dirs: Sequence[Path] = (), prefix: str = "billing_rules") -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmap = code_map_rows(rules)
    rows = main_rows(rules, records)
    ql_cols = ["Rule_ID", "Policy_Title", "Rule_Category", "Audit_Action", "Target_Codes", "Possible_icd_codes",
               "Required_Claim_Fields", "SQL_Query", "Review_Flag"]
    summary = summary_rows(rules, extra_summary)
    summary.insert(0, {"Section": "Rules", "Metric": "Rows in Rules sheet", "Value": len(rows)})
    summary.insert(1, {"Section": "Rules", "Metric": "Clubbed rows (a code with multiple rules in the same policy)",
                       "Value": sum(1 for x in rows if x.get("_clubbed"))})
    summary.insert(2, {"Section": "Rules", "Metric": "Rules with Possible_icd_codes assigned",
                       "Value": sum(1 for x in rows if x.get("Possible_icd_codes"))})
    widths, wrap = RULE_WIDTHS, RULE_WRAP
    sheets = [
        ("Rules", MAIN_COLUMNS, rows, widths, wrap),
        ("Rule_Detail", RULE_COLUMNS, records, RULE_WIDTHS, RULE_WRAP),
        ("Rule_Code_Map", CODE_MAP_COLUMNS, cmap, {"Policy_Title": 34, "Parameter_Value": 30, "Code_Source": 30}, set()),
        ("Query_Logic", ql_cols, rows, RULE_WIDTHS, RULE_WRAP),
    ]
    for s in extra_sheets or []:
        sheets.append(s)
    sheets.append(("Summary", ["Section", "Metric", "Value"], summary,
                   {"Section": 22, "Metric": 60, "Value": 14}, set()))

    paths = {}
    for d in [out_dir] + [Path(x) for x in mirror_dirs]:
        d.mkdir(parents=True, exist_ok=True)
        # Master Excel workbook with all rule sheets (no JSON or CSV formats)
        write_workbook(d / f"{prefix}.xlsx", sheets)
        with open(d / "claim_table_skeleton.sql", "w", encoding="utf-8") as f:
            f.write(sql_skeleton.table_ddl() + "\n")
        paths[f"xlsx:{d}"] = d / f"{prefix}.xlsx"
    return paths
