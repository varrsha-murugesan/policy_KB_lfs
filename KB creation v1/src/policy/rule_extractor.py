"""Deterministic policy-rule extraction for comparison and gap analysis.

Each sentence (or table row) that contains an explicit rule cue becomes a
PolicyRule with a type, polarity and structured attributes (codes, modifiers,
numeric limits, percentages, settings, populations). Sentences without a cue
are not treated as rules. Boilerplate (sentences repeated across many of the
same payer's policies) is marked and excluded from gap findings.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from config.settings import SETTINGS
from src.policy.code_extractor import RX_CPT1, RX_CPT_OTHER, RX_HCPCS, RX_MODIFIER, RX_MODIFIER_SET
from src.utils.ids import stable_id
from src.utils.text_normalizer import normalize, split_sentences

EXTRACTION_RULESET = "rules-1"

# Ordered: first matching type is the primary type; all matches are kept as secondary types.
RULE_PATTERNS = [
    ("prior_authorization", r"\b(prior authori[sz]ation|pre-?authori[sz]ation|precertification|pre-?certification|authori[sz]ation is required|requires? authori[sz]ation|referral or authori[sz]ation)\b"),
    ("exclusion", r"\b(not covered|non-?covered|does not cover|do not cover|excluded|exclusions?|not (?:separately )?(?:reimbursable|payable|eligible)|will not (?:pay|reimburse|allow|cover)|does not (?:reimburse|allow|pay)|deny|denied|denial|not allowed|cannot be billed|not billable)\b"),
    ("frequency_limit", r"\b(up to \d+|no more than \d+|maximum of \d+|limit(?:ed)? to \d+|\d+ (?:sessions?|visits?|units?|times?) (?:per|a|each)|once (?:per|every|a)|per (?:calendar )?(?:year|day|month|week|lifetime|date of service)|(?:every|within) \d+ (?:days?|months?|years?))\b"),
    ("modifier_requirement", r"\b(append(?:ed)?|report(?:ed)? with|requires?|must (?:be )?(?:billed|reported) with|use)\b[^.]{0,60}\bmodifiers?\b|\bmodifiers?\b[^.]{0,60}\b(must|required|should|is not (?:required|appropriate))\b"),
    ("medical_necessity", r"\b(medically necessary|medical necessity|reasonable and necessary|clinically (?:appropriate|indicated)|indications?|criteria (?:are|were) (?:met|satisfied))\b"),
    ("documentation", r"\b(document(?:ation|ed)?|medical records?|must be recorded|signed order|proof of delivery)\b"),
    ("reimbursement", r"\b(reimburs\w*|paid at|payment (?:rate|amount|reduction)|fee schedule|allowable|\d+(?:\.\d+)?\s?%|reduc(?:e|ed|tion)|bundled?|separately payable|rate)\b"),
    ("billing_instruction", r"\b(bill(?:ed|ing)?|report(?:ed|ing)?|submit(?:ted)?|claims? (?:must|should)|ub-?04|cms-?1500|837[ip]?|place of service|revenue code|units? of service)\b"),
    ("coverage", r"\b(cover(?:s|ed|age)?|eligible|benefit|allow(?:s|ed)?|permit(?:s|ted)?)\b"),
]
_RULE_RX = [(t, re.compile(p, re.I)) for t, p in RULE_PATTERNS]

RX_DENY = re.compile(
    r"\b(?:non-?covered|excluded|deny|denied|prohibit\w*)\b"
    r"|\b(?:not|never|no longer)\s+(?:be\s+|been\s+)?(?:separately\s+)?(?:covered|cover|reimburs\w*|payable|paid|pay|"
    r"allowed?|eligible|billable|bill|accepted)\b"
    r"|\b(?:does|do|will|would|cannot|can not|may)\s+not\s+(?:be\s+)?(?:cover|pay|reimburse|allow|accept|bill)\w*\b", re.I)
RX_NOT_REQUIRED = re.compile(r"\b(?:do|does|will|is|are)\s+not\s+(?:require|required|need|necessary)\b|\bno longer requires?\b", re.I)
RX_REQUIRE = re.compile(r"\b(must|required|requires|shall|need to|needs to|mandatory)\b", re.I)
RX_ALLOW = re.compile(r"\b(may|cover(?:s|ed)?|allow(?:s|ed)?|eligible|permit(?:s|ted)?|reimburs\w+)\b", re.I)

RX_NUM_UNIT = re.compile(r"\b(\d+)\s*(sessions?|visits?|units?|days?|hours?|minutes?|times?|months?|years?|treatments?|tests?)\b"
                         r"(?!\s+(?:of age|old|and older|or older))", re.I)
PURPOSE_WORDS = {"screening": r"\bscreen(?:ing)?\b", "diagnostic": r"\bdiagnos(?:is|tic|e|ed)\b",
                 "monitoring": r"\bmonitor(?:ing)?\b|\bsurveillance\b|\bfollow-?up\b",
                 "treatment": r"\btreat(?:ment|ing)?\b|\btherap(?:y|eutic)\b", "preventive": r"\bprevent(?:ive|ion)\b"}
RX_PERCENT = re.compile(r"\b(\d+(?:\.\d+)?)\s?%")
RX_AGE = re.compile(r"\b(?:age[sd]?\s*(?:of\s*)?(\d{1,3})|(\d{1,3})\s*years? (?:of age|old)|(?:under|younger than|older than|over)\s*(?:age\s*)?(\d{1,3}))\b", re.I)
RX_PERIOD = re.compile(r"\bper\s+(calendar\s+year|year|day|date of service|month|week|lifetime|session|visit|encounter)\b|\bonce every\s+(\d+\s+\w+)", re.I)
SETTINGS_WORDS = ["inpatient", "outpatient", "observation", "emergency department", "office", "home health", "home",
                  "telehealth", "skilled nursing", "ambulatory surgical center", "facility", "non-facility",
                  "hospital", "laboratory", "hospice", "nursing facility"]
POPULATION_WORDS = ["medicare advantage", "medicaid", "commercial", "pediatric", "children", "child", "adult", "adults",
                    "newborn", "neonatal", "pregnant", "women", "men", "female", "male", "dual eligible", "transgender"]


@dataclass
class PolicyRule:
    rule_id: str
    policy_uid: str
    section_seq: int
    sentence_idx: int
    rule_type: str
    polarity: str
    scope_label: str | None
    rule_text: str
    normalized_text: str
    attributes: dict
    is_boilerplate: bool
    extraction_rule: str


_AGENCY = re.compile(r"centers for medicare\s*(?:&|and)\s*medicaid services|\bcms\b", re.I)


def _attrs(text: str, secondary: list[str]) -> dict:
    low = _AGENCY.sub(" ", text).lower()
    codes = sorted({m.group(1) for rx in (RX_CPT1, RX_CPT_OTHER, RX_HCPCS) for m in rx.finditer(text)})
    mods = sorted({tok for m in RX_MODIFIER.finditer(text) for tok in re.findall(r"\b[A-Z0-9]{2}\b", m.group(1))}
                  | {m.group(1) + ch for m in RX_MODIFIER_SET.finditer(text) for ch in m.group(2)})
    return {
        "codes": codes,
        "modifiers": mods,
        "quantities": sorted({f"{n} {u.lower().rstrip('s')}" for n, u in RX_NUM_UNIT.findall(text)}),
        "percents": sorted({p for p in RX_PERCENT.findall(text)}),
        "ages": sorted({next(g for g in m.groups() if g) for m in RX_AGE.finditer(text)}),
        "periods": sorted({(a or b).lower() for a, b in RX_PERIOD.findall(text)}),
        "settings": sorted({w for w in SETTINGS_WORDS if re.search(rf"\b{re.escape(w)}\b", low)}),
        "populations": sorted({w for w in POPULATION_WORDS if re.search(rf"\b{re.escape(w)}\b", low)}),
        "purposes": sorted({k for k, rx in PURPOSE_WORDS.items() if re.search(rx, low)}),
        "secondary_types": secondary,
    }


def _polarity(text: str, rtype: str) -> str:
    if RX_NOT_REQUIRED.search(text):
        return "not_required"
    if rtype == "exclusion" or RX_DENY.search(text) and rtype in ("coverage", "reimbursement", "billing_instruction"):
        return "deny"
    if rtype == "frequency_limit":
        return "limit"
    if RX_REQUIRE.search(text):
        return "require"
    if RX_ALLOW.search(text):
        return "allow"
    return "inform"


def classify_sentence(text: str) -> tuple[str | None, list[str]]:
    types = [t for t, rx in _RULE_RX if rx.search(text)]
    return (types[0], types[1:]) if types else (None, [])


def extract_rules(policy) -> list[PolicyRule]:
    rules = []
    for s in policy.sections:
        if not s.content or s.section_role == "definition":
            continue
        if getattr(s, "evidence_location", "policy_text") == "attachment" and not SETTINGS.matching.rules_from_attachments:
            continue  # attachments are mostly code tables; rule extraction there is opt-in
        if s.content_type == "table_row" and s.table_data:
            row = s.table_data.get("row") or {}
            row_vals = " ".join(str(v) for v in row.values())
            # Skip template placeholder rows
            if re.search(r"\b(ddddd|xxxxx)\b", row_vals, re.I):
                continue
            items = []
            for k, v in row.items():
                if not v:
                    continue
                v_clean = re.sub(r"\s+", " ", str(v).replace("\n", ", ")).strip()
                k_clean = str(k).strip()
                if re.match(r"^column_\d+$", k_clean, re.I):
                    items.append(v_clean)
                else:
                    items.append(f"{k_clean}: {v_clean}")
            text = " | ".join(items) or s.content
            if not text:
                continue
            # Skip pure manufacturer entries without billing rules
            if re.match(r"^Code [Dd]escription:\s*Lab/Manufacturer:", text):
                continue
            # Ensure clean terminal punctuation for table rules
            if not text.endswith((".", "!", "?", ";", ")", "\"", "'")):
                text = text + "."
            primary, secondary = classify_sentence(text)
            has_code_col = any(re.search(r"code", k, re.I) for k in row)
            rtype = primary or ("code_applicability" if has_code_col else None)
            if rtype:
                rules.append(_mk(policy, s, 0, text, rtype, secondary, "table_row"))
            continue
        for i, (_a, _b, sent) in enumerate(split_sentences(s.content)):
            sent_clean = sent.strip()
            if len(sent_clean) < 15:
                continue
            # Exclude bare introductory lead-in headers ending with colons
            if sent_clean.endswith(":"):
                continue
            # Exclude table headers or isolated phrases lacking predicates
            if re.fullmatch(r"proposed correct payment amount", sent_clean, re.I):
                continue
            primary, secondary = classify_sentence(sent_clean)
            if primary:
                rules.append(_mk(policy, s, i, sent_clean, primary, secondary, "sentence"))
    return rules


def _mk(policy, s, i, text, rtype, secondary, kind) -> PolicyRule:
    return PolicyRule(
        rule_id=stable_id(policy.policy_uid, s.seq, i, kind), policy_uid=policy.policy_uid, section_seq=s.seq,
        sentence_idx=i, rule_type=rtype, polarity=_polarity(text, rtype), scope_label=s.scope_label,
        rule_text=text, normalized_text=normalize(text), attributes=_attrs(text, secondary), is_boilerplate=False,
        extraction_rule=f"{EXTRACTION_RULESET}:{kind}",
    )


def boilerplate_sentences(policies) -> dict[str, set[str]]:
    """Per payer: normalized sentences that occur in many policies (templates)."""
    cfg = SETTINGS.matching
    by_payer = defaultdict(list)
    for p in policies:
        by_payer[p.payer].append(p)
    out = {}
    for payer, plist in by_payer.items():
        df = Counter()
        for p in plist:
            df.update({normalize(sent) for s in p.sections for _a, _b, sent in split_sentences(s.content) if len(sent) > 30})
        threshold = max(cfg.boilerplate_min_docs, cfg.boilerplate_doc_fraction * len(plist))
        out[payer] = {sent for sent, n in df.items() if n >= threshold}
    return out


def mark_boilerplate(rules: list[PolicyRule], boilerplate: dict[str, set[str]], payer: str) -> None:
    bp = boilerplate.get(payer, set())
    for r in rules:
        r.is_boilerplate = r.normalized_text in bp
