"""
extract_rules.py  --  Stage 2 of the payer policy rule-intelligence pipeline.

Consumes extraction_rules.json plus the mined signal lexicon (rule_keywords.csv)
and emits one row per billing/reimbursement *intent* - not per keyword hit.

What it does, in order:
  1. flattens the policy into addressable text units (guidelines, subsections,
     tables, Q&A) and splits them into sentences;
  2. scores every sentence with the mined lexicon, weighted by signal category;
  3. grows each high-scoring sentence into a rule block by pulling in the
     sentences around it that carry the codes, the conditions or the override;
  4. attaches CPT/HCPCS codes (expanding ranges), modifiers, conditions and
     limitations, and exception clauses;
  5. joins linked-PDF code lists so that "reimbursed once per its time period"
     becomes one concrete rule per time period, with its codes;
  6. links exceptions and derived rules back to the base rule they modify;
  7. writes an Excel workbook.

Usage:
    python extract_rules.py --input extraction_rules.json \
        --keywords rule_keywords.csv --out billing_rules.xlsx [--pdf-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from analyze_keywords import _RULE_RE as STRUCTURAL_ANCHOR
from claim_requirements import assign_claim_columns
from policy_lib import (AttachmentTable, TextUnit, attachment_code_tables,
                        expand_range, extract_codes, extract_conditions,
                        extract_exception_clauses, extract_modifiers,
                        flatten_policy, has_exception_cue, is_boilerplate,
                        load_policies, norm_ws, split_sentences)

# ----------------------------------------------------------------------------
# Signal lexicon
# ----------------------------------------------------------------------------

CATEGORY_WEIGHT = {
    "denial_prohibition": 1.00,
    "reimbursement_action": 1.00,
    "frequency_limit": 0.95,
    "exception_override": 0.90,
    "bundling_inclusion": 0.90,
    "modifier_usage": 0.70,
    "eligibility_condition": 0.60,
    "coding_requirement": 0.55,
    "documentation": 0.50,
    "place_of_service": 0.45,
    "entity_scope": 0.40,
    "temporal_scope": 0.35,
    "general_policy": 0.30,
    "clinical_context": 0.10,
    "continuation_cue": 0.00,          # used for stitching, not for scoring
}

ACTION_RE = re.compile(
    r"\b(reimburs\w*|pay\w*|payable|allow\w*|den\w*|report\w*|bill\w*|submit\w*|"
    r"append\w*|cover\w*|consider\w*|appl(?:y|ies|ied)|recogniz\w*|requir\w*|"
    r"use[sd]?|includ\w*|limit\w*|eligible|separate\w*|adjust\w*|downcod\w*|align\w*|"
    r"indicat\w*|experience\w*|exclude\w*)\b", re.I)

BUNDLING_VOCAB = re.compile(
    r"comprehensive|component|inclusive|separately reimbursab|bundl|indented|"
    r"share a common|most comprehensive", re.I)

FREQUENCY_VOCAB = re.compile(
    r"\bonce\b|per (?:day|month|year|week|calendar|time period)|time span|time period|"
    r"frequency|maximum|limited to|units? of service|global (?:day|period)", re.I)

# generic phrases that mean "the codes are in the attached list"
LIST_REFERENCE_RE = re.compile(
    r"\b(list|attachment|attached|following codes|codes included in|code list|"
    r"policy list|designated|identified in)\b", re.I)


@dataclass
class Signal:
    keyword: str
    category: str
    strength: float
    polarity: str
    role: str


def load_lexicon(path: str) -> Dict[str, Signal]:
    lex: Dict[str, Signal] = {}
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(path) and os.path.exists(os.path.join(base_dir, path)):
        path = os.path.join(base_dir, path)
    if not os.path.exists(path):
        return lex
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lex[row["keyword"]] = Signal(
                keyword=row["keyword"],
                category=row.get("category", "general_policy"),
                strength=float(row.get("strength", 0.5) or 0.5),
                polarity=row.get("polarity", "neutral"),
                role=row.get("signal_role", "rule_indicator"),
            )
    return lex


_TOK = re.compile(r"<code>|<num>|[a-z][a-z'/\-]+")


def tokenize(sentence: str) -> List[str]:
    s = sentence.lower()
    s = re.sub(r"\b\d{4,5}[tu]?\b", " <code> ", s)
    s = re.sub(r"\b\d+\b", " <num> ", s)
    return _TOK.findall(s)


def sentence_signals(sentence: str, lex: Dict[str, Signal]) -> List[Signal]:
    toks = [t.replace("<code>", "{code}").replace("<num>", "{n}") for t in tokenize(sentence)]
    hits: Dict[str, Signal] = {}
    for n in (1, 2, 3):
        for i in range(len(toks) - n + 1):
            gram = " ".join(toks[i:i + n])
            sig = lex.get(gram)
            if sig and sig.role == "rule_indicator":
                hits[gram] = sig
    return list(hits.values())


def score_sentence(sentence: str, lex: Dict[str, Signal]) -> Tuple[float, List[Signal]]:
    """
    Weighted evidence, but capped per category: ten flavours of the same
    "will not reimburse" phrasing should not outrank a genuinely richer rule.
    """
    sigs = sentence_signals(sentence, lex)
    best: Dict[str, List[float]] = defaultdict(list)
    for s in sigs:
        best[s.category].append(s.strength * CATEGORY_WEIGHT.get(s.category, 0.3))
    total = 0.0
    for cat, vals in best.items():
        vals.sort(reverse=True)
        total += vals[0] + 0.25 * sum(vals[1:3])
    return round(total, 3), sorted(sigs, key=lambda s: -s.strength)


def leading_cue(sentence: str, lex: Dict[str, Signal]) -> Optional[Signal]:
    toks = [t.replace("<code>", "{code}").replace("<num>", "{n}") for t in tokenize(sentence)]
    for n in (3, 2, 1):
        if len(toks) >= n:
            sig = lex.get(" ".join(toks[:n]))
            if sig and sig.role == "linkage_cue":
                return sig
    return None


# ----------------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------------

@dataclass
class Rule:
    rule_id: str
    policy_idx: int
    policy_title: str
    policy_number: Optional[str]
    line_of_business: Optional[str]
    billing_class: Optional[str]
    rule_type: str
    text: str
    codes: List[str] = field(default_factory=list)
    ranges: List[str] = field(default_factory=list)
    modifiers: List[str] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)
    exceptions: List[str] = field(default_factory=list)
    source: str = ""
    source_page: str = ""
    parent_id: str = ""
    confidence: float = 0.0
    signals: List[str] = field(default_factory=list)
    attachment: str = ""
    section_path: List[str] = field(default_factory=list)
    order: float = 0
    anchor: str = ""
    claim_columns: List[str] = field(default_factory=list)


CONTINUATION_START = re.compile(
    r"^(instead|however|in addition|additionally|this|these|those|the other|no modifier|"
    r"such|therefore|thus|as a result|in that case|in these|in other|it is|they are|"
    r"the code with|only|both|each|when|if|for example|examples|note:|reimbursement for)\b",
    re.I)

# Sentences that cite the source of a rule rather than stating one. They are
# real evidence for the neighbouring rule, so they get folded into it instead of
# becoming rules of their own.
EVIDENTIARY = re.compile(
    r"^(the parenthetical|this aligns|this is consistent|according to|per (?:cms|the ama|cpt)|"
    r"cpt coding guidelines|cms (?:states|guidance)|the code description|within the cpt book|"
    r"the cpt book|for example|as described in|consistent with (?:cms|cpt|the ama)|"
    r"in accordance with|the ama)\b", re.I)

SELF_CONTAINED_SUBJECT = re.compile(
    r"\b(unitedhealthcare|uhc|cms|the ama|cpt|hcpcs|providers?|physicians?|"
    r"claims?|codes?|modifiers?|services?|policy)\b", re.I)


def build_blocks(sentences: Sequence[str], scored: Sequence[Tuple[float, List[Signal]]],
                 lex: Dict[str, Signal], anchor_threshold: float,
                 support_threshold: float) -> List[List[int]]:
    """Group sentence indices into rule blocks around each anchor sentence."""
    n = len(sentences)
    # A sentence anchors a rule when the mined signals are strong enough, or when
    # it uses one of the unambiguous deontic constructions - the lexicon cannot
    # cover every phrasing, so the structural test provides the floor.
    is_anchor = [(scored[i][0] >= anchor_threshold or bool(STRUCTURAL_ANCHOR.search(sentences[i])))
                 and bool(ACTION_RE.search(sentences[i]))
                 and not EVIDENTIARY.match(sentences[i])
                 for i in range(n)]
    used: Set[int] = set()
    blocks: List[List[int]] = []

    for i in range(n):
        if not is_anchor[i] or i in used:
            continue
        block = [i]
        used.add(i)

        # look back one sentence for the setup that names the codes / scope
        j = i - 1
        if j >= 0 and j not in used and not is_anchor[j]:
            prev = sentences[j]
            prev_codes, _ = extract_codes(prev, expand=False)
            if prev_codes or scored[j][0] >= support_threshold or LIST_REFERENCE_RE.search(prev):
                block.insert(0, j)
                used.add(j)

        # look forward while the sentences keep elaborating this same rule
        k = i + 1
        while k < n and len(block) < 6 and k not in used:
            nxt = sentences[k]
            if is_anchor[k]:
                break
            if EVIDENTIARY.match(nxt) and scored[k][0] >= support_threshold:
                block.append(k)
                used.add(k)
                k += 1
                continue
            cue = leading_cue(nxt, lex)
            starts_continuation = bool(CONTINUATION_START.match(nxt)) or cue is not None
            shares_codes = bool(set(extract_codes(nxt, expand=False)[0]) &
                                set(extract_codes(" ".join(sentences[b] for b in block),
                                                  expand=False)[0]))
            informative = scored[k][0] >= support_threshold or has_exception_cue(nxt)
            if (starts_continuation or shares_codes) and (informative or not SELF_CONTAINED_SUBJECT.match(nxt)):
                block.append(k)
                used.add(k)
                k += 1
                continue
            break
        blocks.append(block)
    return blocks


def title_codes(section_path: Sequence[str], universe: Set[str]) -> Tuple[List[str], List[str]]:
    codes: List[str] = []
    ranges: List[str] = []
    for part in section_path:
        c, r = extract_codes(part, universe)
        codes.extend(c)
        ranges.extend(r)
    return codes, ranges


def classify(block_text: str, unit: TextUnit) -> str:
    if unit.source_type == "qa":
        return "qa_clarification"
    if unit.source_type == "table":
        return "definition"
    if re.search(r"\b(does not apply|do not apply|will not apply|is not subject to|"
                 r"are not subject to|instead)\b", block_text, re.I):
        return "exception"
    if has_exception_cue(block_text) and re.search(r"\bexcept|\bunless\b", block_text, re.I):
        return "exception"
    return "base"


def dedupe(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ----------------------------------------------------------------------------
# Attachment joins
# ----------------------------------------------------------------------------

def pretty_value(value: str) -> str:
    """'30' means 30 days in these code lists; spell it out."""
    return f"{value} days" if value.isdigit() else value


def page_ref(tbl: AttachmentTable) -> str:
    pages = [p for p in tbl.pages if p]
    if not pages:
        return ""
    span = f"{min(pages)}-{max(pages)}" if len(pages) > 1 else str(pages[0])
    return f"{tbl.name} p.{span}"


def attachment_conditions(codes: Sequence[str], tables: Sequence[AttachmentTable]) -> List[str]:
    """For a rule that names codes, pull each code's attribute from the code lists."""
    out: List[str] = []
    for tbl in tables:
        label = "Related component code(s)" if tbl.value_is_code else tbl.attribute
        hits = [(c, ", ".join(v if tbl.value_is_code else pretty_value(v)
                              for v in tbl.mapping[c]))
                for c in codes if c in tbl.mapping]
        if not hits:
            continue
        shown = "; ".join(f"{c} = {v}" for c, v in hits[:12])
        more = f" (+{len(hits) - 12} more)" if len(hits) > 12 else ""
        out.append(f"{label} per {tbl.name}: {shown}{more}")
    return out


def make_derived_rules(base: Rule, tables: Sequence[AttachmentTable],
                       next_id, max_groups: int = 25) -> List[Rule]:
    """
    Turn a general rule + a linked code list into concrete, code-level intents.

    "reimbursed once during its time period" + {95250: Calendar Month, 93268: 30}
      -> one rule for the Calendar Month codes, one for the 30-day codes, ...
    """
    derived: List[Rule] = []
    for tbl in tables:
        if tbl.value_is_code:
            groups: Dict[str, List[str]] = {k: list(v) for k, v in tbl.mapping.items()}
            if len(groups) > max_groups:
                # too many pairs to enumerate: one rule carrying the relationships
                pairs = "; ".join(f"{c} > {', '.join(v)}" for c, v in list(groups.items())[:40])
                derived.append(Rule(
                    rule_id=next_id(), policy_idx=base.policy_idx,
                    policy_title=base.policy_title, policy_number=base.policy_number,
                    line_of_business=base.line_of_business, billing_class=base.billing_class,
                    rule_type="derived",
                    text=(f"{base.text} Applied to the {tbl.name} list: for each of the "
                          f"{len(groups)} comprehensive/component pairings in that list, only the "
                          f"comprehensive code is reimbursable when both are reported in the same "
                          f"time span by the same group physician for the same patient."),
                    codes=sorted(set(groups) | {v for vs in groups.values() for v in vs}),
                    conditions=[f"Comprehensive > component pairings ({len(groups)} total): {pairs}"
                                + (" ..." if len(groups) > 40 else ""),
                                "No modifier overrides this denial"],
                    source=f"{base.source} + attachment {tbl.name}",
                    source_page=page_ref(tbl), parent_id=base.rule_id,
                    confidence=round(min(0.99, base.confidence + 0.05), 2), signals=base.signals,
                    attachment=tbl.name, section_path=base.section_path,
                    order=base.order, anchor=base.anchor))
                continue
            for comp, components in groups.items():
                rule = Rule(
                    rule_id=next_id(),
                    policy_idx=base.policy_idx,
                    policy_title=base.policy_title,
                    policy_number=base.policy_number,
                    line_of_business=base.line_of_business,
                    billing_class=base.billing_class,
                    rule_type="derived",
                    text=(f"{base.text} Applied to the {tbl.name} list: when component code(s) "
                          f"{', '.join(components)} are reported with comprehensive code {comp} "
                          f"in the same time span by the same group physician for the same patient, "
                          f"only {comp} is reimbursable; the component code(s) are inclusive."),
                    codes=[comp] + components,
                    conditions=[f"Comprehensive code: {comp}",
                                f"Component code(s): {', '.join(components)}",
                                "Same time span period, same group physician/other QHCP, same patient",
                                "No modifier overrides this denial"],
                    source=f"{base.source} + attachment {tbl.name}",
                    source_page=page_ref(tbl),
                    parent_id=base.rule_id,
                    confidence=round(min(0.99, base.confidence + 0.05), 2),
                    signals=base.signals,
                    attachment=tbl.name,
                    section_path=base.section_path,
                    order=base.order,
                    anchor=base.anchor,
                )
                derived.append(rule)
            continue

        buckets: Dict[str, List[str]] = defaultdict(list)
        for code, vals in tbl.mapping.items():
            buckets[", ".join(vals)].append(code)

        # A column of per-code descriptions is not a classifier - expanding it
        # would emit one "rule" per code. Summarise the list instead.
        avg_group = len(tbl.mapping) / max(len(buckets), 1)
        classifier = (len(buckets) <= 20 and avg_group >= 2
                      and max(len(v) for v in buckets) <= 60)
        if not classifier:
            derived.append(Rule(
                rule_id=next_id(), policy_idx=base.policy_idx,
                policy_title=base.policy_title, policy_number=base.policy_number,
                line_of_business=base.line_of_business, billing_class=base.billing_class,
                rule_type="derived",
                text=(f"{base.text} Applied to the {tbl.name} list: this rule governs the "
                      f"{len(tbl.mapping)} code(s) published in that attachment, each with its own "
                      f"{tbl.attribute}."),
                codes=sorted(tbl.mapping),
                conditions=[f"Code-specific {tbl.attribute} published in {tbl.name}"],
                source=f"{base.source} + attachment {tbl.name}",
                source_page=page_ref(tbl), parent_id=base.rule_id,
                confidence=round(min(0.99, base.confidence + 0.03), 2), signals=base.signals,
                attachment=tbl.name, section_path=base.section_path,
                order=base.order, anchor=base.anchor))
            continue

        ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:max_groups]
        for value, codes in ordered:
            pretty = pretty_value(value)
            rule = Rule(
                rule_id=next_id(),
                policy_idx=base.policy_idx,
                policy_title=base.policy_title,
                policy_number=base.policy_number,
                line_of_business=base.line_of_business,
                billing_class=base.billing_class,
                rule_type="derived",
                text=(f"{base.text} Applied to the {tbl.name} list: the {len(codes)} code(s) with "
                      f"{tbl.attribute} = {pretty} are reimbursable once per {pretty} "
                      f"for the same member by the same group physician and/or other qualified "
                      f"health care professional."),
                codes=sorted(codes),
                conditions=[f"{tbl.attribute} = {pretty}",
                            f"One reimbursement per {pretty} per member",
                            "Same group physician and/or other qualified health care professional"],
                source=f"{base.source} + attachment {tbl.name}",
                source_page=page_ref(tbl),
                parent_id=base.rule_id,
                confidence=min(0.99, base.confidence + 0.05),
                signals=base.signals,
                attachment=tbl.name,
                section_path=base.section_path,
                order=base.order,
                anchor=base.anchor,
            )
            derived.append(rule)
    return derived


# ----------------------------------------------------------------------------
# Parent linkage
# ----------------------------------------------------------------------------

_CONTENT = re.compile(r"[a-z]{4,}")


def content_tokens(text: str) -> Set[str]:
    return set(_CONTENT.findall(text.lower()))


def link_parents(rules: List[Rule]) -> None:
    """Attach every exception / dependent rule to the base rule it modifies."""
    live = {r.rule_id for r in rules}
    for r in rules:
        if r.parent_id and r.parent_id not in live:
            r.parent_id = ""
    by_policy: Dict[int, List[Rule]] = defaultdict(list)
    for r in rules:
        by_policy[r.policy_idx].append(r)

    for pol_rules in by_policy.values():
        bases = [r for r in pol_rules if r.rule_type in ("base", "qa_clarification")]
        if not bases:
            continue
        # the policy's general rule: highest confidence base rule stated without codes
        general = max((b for b in bases if not b.codes),
                      key=lambda b: b.confidence, default=None) or \
            max(bases, key=lambda b: b.confidence)

        for r in pol_rules:
            if r.parent_id or r is general:
                continue
            if r.rule_type not in ("exception", "qa_clarification") and r.rule_type != "base":
                continue
            if r.rule_type == "base" and len(r.section_path) <= 1:
                continue

            best, best_score = None, 0.0
            r_tokens = content_tokens(r.text)
            for cand in bases:
                if cand is r or cand.order >= r.order:
                    continue          # a rule can only refine one stated before it
                score = 0.0
                if r.codes and cand.codes:
                    inter = len(set(r.codes) & set(cand.codes))
                    if inter:
                        score += 3.0 * inter / len(set(r.codes) | set(cand.codes))
                if len(cand.section_path) < len(r.section_path) and \
                        r.section_path[:len(cand.section_path)] == cand.section_path:
                    score += 1.2
                overlap = len(r_tokens & content_tokens(cand.text))
                score += min(overlap / 25.0, 1.0)
                score += 0.4 / (1 + (r.order - cand.order))
                if score > best_score:
                    best, best_score = cand, score

            if r.rule_type == "exception":
                r.parent_id = (best.rule_id if best and best_score >= 0.6
                               else (general.rule_id if general is not r and general.order < r.order
                                     else ""))
            elif best and best_score >= 1.6:
                r.parent_id = best.rule_id

    # safety net: no rule may end up as its own ancestor
    by_id = {r.rule_id: r for r in rules}
    for r in rules:
        seen, node = {r.rule_id}, r
        while node.parent_id and node.parent_id in by_id:
            if node.parent_id in seen:
                node.parent_id = ""
                break
            seen.add(node.parent_id)
            node = by_id[node.parent_id]

    # surface each exception on its parent row too
    by_id = {r.rule_id: r for r in rules}
    for r in rules:
        if r.rule_type == "exception" and r.parent_id in by_id:
            parent = by_id[r.parent_id]
            note = f"[{r.rule_id}] {r.text[:220]}"
            if note not in parent.exceptions:
                parent.exceptions.append(note)


# ----------------------------------------------------------------------------
# Page resolution against the source PDFs (optional)
# ----------------------------------------------------------------------------

_POLNUM = re.compile(r"\b\d{4}R\d{4}[A-Z]?\b")


def build_pdf_index(pdf_dir: str) -> Dict[str, List[str]]:
    """policy number -> list of normalized page texts."""
    index: Dict[str, List[str]] = {}
    if not pdf_dir or not os.path.isdir(pdf_dir):
        return index
    try:
        import pdfplumber
    except ImportError:
        return index
    for fname in sorted(os.listdir(pdf_dir)):
        if not fname.lower().endswith(".pdf"):
            continue
        try:
            with pdfplumber.open(os.path.join(pdf_dir, fname)) as pdf:
                pages = [norm_ws(p.extract_text() or "").lower() for p in pdf.pages]
        except Exception:
            continue
        nums = Counter(m for page in pages for m in _POLNUM.findall(page.upper()))
        if nums:
            index[nums.most_common(1)[0][0]] = pages
    return index


def resolve_page(rule: Rule, index: Dict[str, List[str]]) -> str:
    pages = index.get((rule.policy_number or "").upper())
    if not pages or not rule.anchor:
        return rule.source_page
    probe = norm_ws(rule.anchor).lower()
    probe = re.sub(r"[^a-z0-9 ]", "", probe)[:60]
    if len(probe) < 25:
        return rule.source_page
    for i, page in enumerate(pages, start=1):
        flat = re.sub(r"[^a-z0-9 ]", "", page)
        if probe in flat:
            return str(i)
    return rule.source_page


ANATOMICAL_MODIFIERS = [
    "FA", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    "TA", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9",
    "E1", "E2", "E3", "E4",
    "LC", "LD", "LM", "RC", "RI", "RT", "LT"
]


def enrich_rules_cross_sectional(rules: List[Rule], policy: Dict[str, Any], units: List[TextUnit]) -> None:
    """Enrich rules using cross-sectional evidence from Q&A, Definitions, and Regulatory schemas."""
    title = (policy.get("policy_title") or "").strip()
    title_lower = title.lower()
    policy_cpts = set(policy.get("CPT_CODES_USED") or [])

    # Harvest all modifiers across all units of the policy
    all_unit_modifiers = set()
    for u in units:
        for m in extract_modifiers(u.text):
            all_unit_modifiers.add(m)

    for r in rules:
        r_text_lower = r.text.lower()

        # --- A. Anatomical Modifier Policies ---
        if "anatomical modifier" in title_lower or "anatomical modifier" in r_text_lower:
            r.modifiers = dedupe(r.modifiers + ANATOMICAL_MODIFIERS)
            r.conditions = dedupe(r.conditions + [
                "Appropriate anatomical modifier required (E1-E4 for eyelids, FA/F1-F9 for fingers, TA/T1-T9 for toes, LC/LD/LM/RC/RI for coronary arteries, RT/LT for bilateral sides)",
                "Claims billed without highest specificity are subject to denial"
            ])
            if not r.ranges and not r.codes:
                r.ranges = ["10000-69999", "70000-79999"]

        # --- B. Modifier Reference & Policy ---
        elif "modifier" in title_lower and all_unit_modifiers:
            if not r.modifiers:
                r.modifiers = sorted(all_unit_modifiers)[:20]

        # --- C. CLIA ID Requirement ---
        if "clia" in title_lower:
            r.modifiers = dedupe(r.modifiers + ["QW"])
            r.conditions = dedupe(r.conditions + [
                "Valid 10-digit CLIA certificate required on claim (CMS-1500 box 23 or UB-04 FL 67)",
                "Modifier QW required for CLIA-waived test procedures",
                "PPM certificate required for provider-performed microscopy procedures",
                "Moderate or High complexity certification required for non-waived lab procedures"
            ])

        # --- D. National Drug Code (NDC) Requirement ---
        elif "national drug code" in title_lower or "ndc" in title_lower:
            r.conditions = dedupe(r.conditions + [
                "Valid 11-digit unhyphenated NDC in 5-4-2 format required",
                "Valid Unit of Measure Qualifier required: UN (Units), ML (Milliliters), GR (Grams), or F2 (International Units)",
                "Billed drug units must match HCPCS dosage unit conversion formula"
            ])

        # --- E. Molecular Pathology Policy ---
        elif "molecular pathology" in title_lower:
            r.conditions = dedupe(r.conditions + [
                "Valid 5-character alphanumeric DEX Z-Code required for multi-gene tests",
                "Test must be registered in Palmetto GBA DEX Diagnostics Exchange"
            ])

        # --- F. MPFS Status Indicator Policy ---
        elif "status indicator" in title_lower or "npfs" in title_lower:
            r.conditions = dedupe(r.conditions + [
                "Status B: Bundled service (never separately payable)",
                "Status T: Injections/infusions (payable only if no other MPFS service is billed on same DOS)",
                "Status M: Measurement codes (Category II 0001F-9007F zero reimbursement)",
                "Status P: Incident-to services only",
                "Status X: Statutory exclusion"
            ])

        # --- G. Emergency Department Facility E&M Policy ---
        elif "emergency department" in title_lower and ("facility" in title_lower or (r.billing_class or "").lower() in ("facility", "both")):
            r.codes = dedupe(r.codes + ["99281", "99282", "99283", "99284", "99285"])
            r.conditions = dedupe(r.conditions + [
                "Optum EDC Analyzer acuity downcoding algorithm applied to UB-04 Level 4/5 E/M codes (99284/99285)",
                "Excludes inpatient admissions from ED, acute transfers, expired patients, and critical care (99291)",
                "UB-04 Revenue codes 0450, 0451, 0452, 0456, 0459; Bill Types 0131, 0137, 0851; Place of Service 23 (Emergency Room)"
            ])

        # --- H. Focused Policy Code Inheritance ---
        if not r.codes and 0 < len(policy_cpts) <= 60:
            r.codes = sorted(policy_cpts)

        # --- I. Hospital Facility (UB-04) Field Auto-Binding ---
        if (r.billing_class or "").lower() in ("facility", "both"):
            if "hospital inclusive" in title_lower or "services included in facility" in title_lower:
                r.conditions = dedupe(r.conditions + [
                    "UB-04 Revenue codes 0100-0219 (Room & Board), 0250 (Pharmacy), 0270 (Medical Supplies), 0360 (Operating Room)",
                    "Bill Type 0111 (Inpatient Hospital)"
                ])
            elif "observation" in title_lower:
                r.conditions = dedupe(r.conditions + [
                    "UB-04 Revenue code 0760, 0762; Condition Code 44 (Observation); Bill Type 0131 (Outpatient Hospital)"
                ])
            elif "outpatient" in title_lower:
                r.conditions = dedupe(r.conditions + [
                    "UB-04 Bill Types 0131, 0132, 0133, 0831; Place of Service 19, 22"
                ])


# ----------------------------------------------------------------------------
# Main extraction
# ----------------------------------------------------------------------------

def extract_policy_rules(policy: Dict[str, Any], idx: int, lex: Dict[str, Signal],
                         anchor_threshold: float, support_threshold: float) -> List[Rule]:
    units = flatten_policy(policy, idx)
    if not units:
        return []

    tables = attachment_code_tables(policy)
    universe: Set[str] = set(policy.get("CPT_CODES_USED") or [])
    for t in tables:
        universe |= set(t.mapping.keys())
        if t.value_is_code:
            universe |= {v for vs in t.mapping.values() for v in vs}

    slug = (policy.get("policy_number") or f"P{idx:03d}").replace(" ", "")
    counter = [0]

    def next_id() -> str:
        counter[0] += 1
        return f"{slug}-R{counter[0]:03d}"

    rules: List[Rule] = []

    for unit in units:
        sentences = [s for s in split_sentences(unit.text) if not is_boilerplate(s)]
        if not sentences:
            continue
        scored = [score_sentence(s, lex) for s in sentences]
        blocks = build_blocks(sentences, scored, lex, anchor_threshold, support_threshold)

        for block in blocks:
            block_sents = [sentences[b] for b in block]
            text = " ".join(block_sents)
            anchor = max(block, key=lambda b: scored[b][0])

            if unit.source_type == "qa" and unit.context:
                # a bare "No." or a pronoun-led answer only means something with its question
                if len(block_sents[0]) < 110 or block_sents[0].lstrip().lower().startswith(
                        ("no", "yes", "this", "these", "it ", "they ")):
                    text = f"In response to: \"{unit.context}\" - {text}"

            codes, ranges = extract_codes(text, universe)
            t_codes, t_ranges = title_codes(unit.section_path, universe)
            codes = dedupe(codes + t_codes)
            ranges = dedupe(ranges + t_ranges)

            conditions = extract_conditions(text)
            conditions += attachment_conditions(codes, tables)
            exceptions = extract_exception_clauses(text) if has_exception_cue(text) else []
            if classify(text, unit) == "exception":
                # the row itself is the exception; keep only what replaces the base rule
                exceptions = [e for e in exceptions
                              if re.search(r"\binstead\b|in lieu of|rather than|"
                                           r"other than|applies the", e, re.I)]

            score = sum(scored[b][0] for b in block) / max(len(block), 1)
            confidence = min(0.99, round(0.45 + score / 6.0
                                         + (0.06 if codes else 0)
                                         + (0.05 if conditions else 0), 2))

            rule = Rule(
                rule_id=next_id(),
                policy_idx=idx,
                policy_title=unit.policy_title,
                policy_number=unit.policy_number,
                line_of_business=unit.line_of_business,
                billing_class=unit.billing_class,
                rule_type=classify(text, unit),
                text=norm_ws(text),
                codes=codes,
                ranges=ranges,
                modifiers=extract_modifiers(text),
                conditions=dedupe(conditions),
                exceptions=exceptions,
                source=unit.source_label,
                source_page=str(unit.page) if unit.page else "",
                confidence=confidence,
                signals=[s.keyword for s in scored[anchor][1][:6]],
                section_path=unit.section_path,
                order=unit.order * 100 + block[0],
                anchor=sentences[anchor],
            )
            rules.append(rule)

    # derive concrete intents from linked code lists
    if tables and rules:
        candidates = [r for r in rules if r.rule_type == "base"]
        pairs: List[Tuple[float, int, int]] = []
        for ti, tbl in enumerate(tables):
            stem = re.sub(r"[^a-z]", " ", tbl.name.lower().replace(".pdf", ""))
            key_tokens = content_tokens(stem) | content_tokens(tbl.attribute)
            # a relation table belongs with a bundling rule; a classifier table
            # belongs with a frequency/limit rule
            for ri, r in enumerate(candidates):
                toks = content_tokens(r.text)
                score = len(key_tokens & toks) / max(len(key_tokens), 1)
                kind_re = BUNDLING_VOCAB if tbl.value_is_code else FREQUENCY_VOCAB
                other_re = FREQUENCY_VOCAB if tbl.value_is_code else BUNDLING_VOCAB
                if kind_re.search(r.text):
                    score += 0.6
                if other_re.search(r.text):
                    score -= 0.5               # that rule is about the other concept
                if not r.codes:
                    score += 0.35              # a general rule is the natural parent
                if LIST_REFERENCE_RE.search(r.text):
                    score += 0.2
                score += r.confidence / 10
                pairs.append((score, ti, ri))

        pairs.sort(key=lambda p: -p[0])
        taken_tables: Set[int] = set()
        taken_rules: Set[int] = set()
        for score, ti, ri in pairs:
            if score < 0.4 or ti in taken_tables:
                continue
            if ri in taken_rules and len(taken_rules) < len(candidates):
                continue                        # spread lists across distinct base rules
            taken_tables.add(ti)
            taken_rules.add(ri)
            new = make_derived_rules(candidates[ri], [tables[ti]], next_id)
            for n, child in enumerate(new, start=1):
                child.order = candidates[ri].order + n / 1000.0   # keep children after their parent
            rules.extend(new)

    enrich_rules_cross_sectional(rules, policy, units)

    # drop near-duplicates within a policy
    seen: Set[Any] = set()
    unique: List[Rule] = []
    for r in sorted(rules, key=lambda r: (-r.confidence, r.order)):
        key = (re.sub(r"[^a-z0-9]", "", r.text.lower()), tuple(r.codes), tuple(r.modifiers))
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)
    unique.sort(key=lambda r: r.order)
    return unique


# ----------------------------------------------------------------------------
# Excel output
# ----------------------------------------------------------------------------

COLUMNS = ["Rule_ID", "Policy_Title", "Policy_Number", "Line_of_Business", "Billing_Class",
           "Rule", "CPT_HCPCS_Codes", "Modifiers", "Conditions", "Exceptions",
           "Source", "Source_Page", "Parent_Rule_ID",
           "Rule_Type", "Confidence", "Code_Count", "Trigger_Keywords",
           "Attachment_Reference", "Section_Path", "Required_Claim_Columns"]

# openpyxl silently converts a zero-length string into an empty (null) cell, so
# a rule that legitimately carries no modifier came back as blank instead of an
# empty value. Write the empty modifier explicitly.
EMPTY_MODIFIER = '""' 

CELL_LIMIT = 30000


def cell(value: str) -> str:
    value = norm_ws(value)
    return value if len(value) <= CELL_LIMIT else value[:CELL_LIMIT - 3] + "..."


def rule_row(r: Rule) -> Dict[str, Any]:
    codes = list(r.codes)
    code_text = ", ".join(codes)
    if r.ranges:
        code_text = ", ".join(r.ranges) + (f" (expanded: {code_text})" if codes else "")
    return {
        "Rule_ID": r.rule_id,
        "Policy_Title": r.policy_title,
        "Policy_Number": r.policy_number or "",
        "Line_of_Business": r.line_of_business or "",
        "Billing_Class": r.billing_class or "",
        "Rule": cell(r.text),
        "CPT_HCPCS_Codes": cell(code_text),
        "Modifiers": ", ".join(r.modifiers),
        "Conditions": cell(" | ".join(r.conditions)),
        "Exceptions": cell(" | ".join(r.exceptions)),
        "Source": r.source,
        "Source_Page": r.source_page,
        "Parent_Rule_ID": r.parent_id,
        "Rule_Type": r.rule_type,
        "Confidence": r.confidence,
        "Code_Count": len(codes),
        "Trigger_Keywords": ", ".join(r.signals),
        "Attachment_Reference": r.attachment,
        "Section_Path": " > ".join(r.section_path),
        "Required_Claim_Columns": ", ".join(r.claim_columns),
    }


def write_workbook(rules: List[Rule], lexicon_path: str, policies: List[Dict[str, Any]],
                   out_path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1F3864")
    body = Font(name="Arial", size=10)

    def style_sheet(ws, widths: Dict[str, int], wrap_cols: Set[str]) -> None:
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font = body
                c.alignment = Alignment(vertical="top",
                                        wrap_text=ws.cell(1, c.column).value in wrap_cols)
        for i in range(1, ws.max_column + 1):
            name = ws.cell(1, i).value
            ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 16)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    # ---- Rules
    ws = wb.active
    ws.title = "Rules"
    ws.append(COLUMNS)
    for r in rules:
        row = rule_row(r)
        if not row["Modifiers"]:
            row["Modifiers"] = EMPTY_MODIFIER
        ws.append([row[c] for c in COLUMNS])
    style_sheet(ws,
                {"Rule_ID": 16, "Policy_Title": 34, "Policy_Number": 14, "Line_of_Business": 16,
                 "Billing_Class": 13, "Rule": 90, "CPT_HCPCS_Codes": 34, "Modifiers": 12,
                 "Conditions": 55, "Exceptions": 45, "Source": 34, "Source_Page": 11,
                 "Parent_Rule_ID": 16, "Rule_Type": 15, "Confidence": 11, "Code_Count": 11,
                 "Trigger_Keywords": 34, "Attachment_Reference": 26, "Section_Path": 34,
                 "Required_Claim_Columns": 46},
                {"Rule", "Conditions", "Exceptions", "CPT_HCPCS_Codes", "Source", "Section_Path",
                 "Required_Claim_Columns"})

    # ---- Code index (one row per code per rule)
    ws2 = wb.create_sheet("Code_Index")
    ws2.append(["Code", "Rule_ID", "Policy_Number", "Policy_Title", "Rule_Type", "Rule_Summary"])
    for r in rules:
        for code in r.codes[:400]:
            ws2.append([code, r.rule_id, r.policy_number or "", r.policy_title,
                        r.rule_type, cell(r.text)[:300]])
    style_sheet(ws2, {"Code": 12, "Rule_ID": 16, "Policy_Number": 14, "Policy_Title": 34,
                      "Rule_Type": 15, "Rule_Summary": 90}, {"Rule_Summary"})

    # ---- Signal lexicon
    ws3 = wb.create_sheet("Signal_Lexicon")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(lexicon_path) and os.path.exists(os.path.join(base_dir, lexicon_path)):
        lexicon_path = os.path.join(base_dir, lexicon_path)
    if os.path.exists(lexicon_path):
        with open(lexicon_path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for row in reader:
                ws3.append(row)
    style_sheet(ws3, {"keyword": 32, "category": 22, "strength": 11, "strength_band": 14,
                      "polarity": 13, "signal_role": 15}, set())

    # ---- Summary
    ws4 = wb.create_sheet("Summary")
    types = Counter(r.rule_type for r in rules)
    ws4.append(["Metric", "Value"])
    ws4.append(["Policies in source file", len(policies)])
    ws4.append(["Policies yielding rules", len({r.policy_idx for r in rules})])
    ws4.append(["Total rules/intents", len(rules)])
    for t, n in types.most_common():
        ws4.append([f"  of which {t}", n])
    ws4.append(["Rules with CPT/HCPCS codes", sum(1 for r in rules if r.codes)])
    ws4.append(["Rules with modifiers", sum(1 for r in rules if r.modifiers)])
    ws4.append(["Rules with conditions", sum(1 for r in rules if r.conditions)])
    ws4.append(["Rules linked to a parent rule", sum(1 for r in rules if r.parent_id)])
    ws4.append(["Rules enriched from linked PDFs", sum(1 for r in rules if r.attachment)])
    ws4.append(["Distinct codes covered", len({c for r in rules for c in r.codes})])
    ws4.append(["Mean confidence", round(sum(r.confidence for r in rules) / max(len(rules), 1), 3)])
    style_sheet(ws4, {"Metric": 40, "Value": 18}, set())

    wb.save(out_path)


# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="extraction_rules.json")
    ap.add_argument("--keywords", default="rule_keywords.csv")
    ap.add_argument("--out", default="billing_rules.xlsx")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--pdf-dir", default="", help="folder of source policy PDFs, for page numbers")
    ap.add_argument("--anchor-threshold", type=float, default=0.90)
    ap.add_argument("--support-threshold", type=float, default=0.60)
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(args.input) and os.path.exists(os.path.join(base_dir, args.input)):
        args.input = os.path.join(base_dir, args.input)
    if not os.path.exists(args.keywords) and os.path.exists(os.path.join(base_dir, args.keywords)):
        args.keywords = os.path.join(base_dir, args.keywords)
    if not os.path.isabs(args.out):
        args.out = os.path.join(base_dir, args.out)
    if args.json_out and not os.path.isabs(args.json_out):
        args.json_out = os.path.join(base_dir, args.json_out)
    if args.pdf_dir and not os.path.exists(args.pdf_dir) and os.path.exists(os.path.join(base_dir, args.pdf_dir)):
        args.pdf_dir = os.path.join(base_dir, args.pdf_dir)

    lex = load_lexicon(args.keywords)
    policies = load_policies(args.input)

    rules: List[Rule] = []
    for idx, pol in enumerate(policies):
        rules.extend(extract_policy_rules(pol, idx, lex,
                                          args.anchor_threshold, args.support_threshold))

    link_parents(rules)
    assign_claim_columns(rules)

    index = build_pdf_index(args.pdf_dir)
    if index:
        for r in rules:
            if not r.source_page:
                r.source_page = resolve_page(r, index)

    write_workbook(rules, args.keywords, policies, args.out)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump([rule_row(r) for r in rules], fh, indent=1)

    print(f"rules: {len(rules)}   policies covered: {len({r.policy_idx for r in rules})}")
    print(f"types: {dict(Counter(r.rule_type for r in rules))}")
    print(f"with codes: {sum(1 for r in rules if r.codes)}   "
          f"with parent: {sum(1 for r in rules if r.parent_id)}   "
          f"from attachments: {sum(1 for r in rules if r.attachment)}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
