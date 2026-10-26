"""
policy_lib.py
Shared utilities for the payer-policy rule-intelligence pipeline.

Handles the messy parts that both stages need:
  * flattening extraction_rules.json into addressable text units
  * sentence segmentation that survives "e.g.,", code ranges and CPT parentheticals
  * CPT / HCPCS / modifier recognition
  * boilerplate detection (disclaimers, history, navigation junk)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ----------------------------------------------------------------------------
# Text units
# ----------------------------------------------------------------------------


@dataclass
class TextUnit:
    """One addressable block of policy prose."""

    policy_idx: int
    policy_title: str
    policy_number: Optional[str]
    line_of_business: Optional[str]
    billing_class: Optional[str]
    source_type: str          # guideline | subsection | table | qa | attachment
    section_path: List[str]   # breadcrumb, outermost first
    text: str
    url: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    context: str = ""      # e.g. the question a Q&A answer belongs to

    @property
    def source_label(self) -> str:
        crumb = " > ".join(self.section_path) if self.section_path else "Reimbursement Guidelines"
        if self.source_type == "qa":
            return f"Questions and Answers > {crumb}"
        if self.source_type == "attachment":
            name = (self.url or "").rsplit("/", 1)[-1] or "attachment"
            return f"Attachment: {name}"
        return crumb


# ----------------------------------------------------------------------------
# Loading / flattening
# ----------------------------------------------------------------------------

_WS = re.compile(r"[ \t\u00a0]+")
_NL = re.compile(r"\s*\n\s*")


def norm_ws(text: Any) -> str:
    if text is None or text == "":
        return ""
    if isinstance(text, dict):
        # some extracted tables embed Q/A objects instead of plain strings
        if "Q" in text or "A" in text:
            text = f"Q: {text.get('Q', '')} A: {text.get('A', '')}"
        else:
            text = " ".join(f"{k}: {v}" for k, v in text.items())
    elif isinstance(text, (list, tuple)):
        text = " ".join(norm_ws(t) for t in text)
    elif not isinstance(text, str):
        text = str(text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = _NL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def load_policies(path: str) -> List[Dict[str, Any]]:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(path) and os.path.exists(os.path.join(base_dir, path)):
        path = os.path.join(base_dir, path)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _subsection_units(
    node: Dict[str, Any],
    base: Dict[str, Any],
    path: List[str],
    counter: List[int],
) -> List[TextUnit]:
    """Recursively walk a subsection tree, emitting prose and key/value tables."""
    units: List[TextUnit] = []
    title = norm_ws(node.get("title")) or "Section"
    here = path + [title]

    content = norm_ws(node.get("content"))
    child_texts = [norm_ws(c.get("content")) for c in (node.get("subsections") or [])]
    content = _strip_duplicated_children(content, child_texts)
    if content:
        counter[0] += 1
        units.append(TextUnit(source_type="subsection", section_path=here,
                              text=content, order=counter[0], **base))

    for tbl in node.get("tables") or []:
        rows = tbl.get("content") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key, val = norm_ws(row.get("key")), norm_ws(row.get("value"))
            if not (key and val):
                continue
            counter[0] += 1
            units.append(TextUnit(source_type="table", section_path=here + [key],
                                  text=f"{key}: {val}", order=counter[0], **base))

    for child in node.get("subsections") or []:
        units.extend(_subsection_units(child, base, here, counter))
    return units


def _strip_duplicated_children(parent: str, children: Iterable[str]) -> str:
    """The extractor repeats child text inside the parent blob; drop the echo."""
    if not parent:
        return ""
    out = parent
    for child in children:
        if child and len(child) > 40 and child in out:
            out = out.replace(child, " ")
    out = norm_ws(out)
    return "" if len(out) < 40 else out


def flatten_policy(policy: Dict[str, Any], idx: int) -> List[TextUnit]:
    """Turn one policy record into an ordered list of TextUnits."""
    base = dict(
        policy_idx=idx,
        policy_title=norm_ws(policy.get("policy_title")) or "Untitled Policy",
        policy_number=norm_ws(policy.get("policy_number")) or None,
        line_of_business=norm_ws(policy.get("Line_of_Business")) or None,
        billing_class=norm_ws(policy.get("Billingclass")) or None,
    )
    counter = [0]
    units: List[TextUnit] = []

    rg = policy.get("reimbursement_guidelines") or {}
    subs = rg.get("subsections") or []
    top = _strip_duplicated_children(norm_ws(rg.get("content")),
                                     [norm_ws(s.get("content")) for s in subs])
    if top:
        counter[0] += 1
        units.append(TextUnit(source_type="guideline",
                              section_path=[norm_ws(rg.get("title")) or "Reimbursement Guidelines"],
                              text=top, order=counter[0], **base))

    for sub in subs:
        units.extend(_subsection_units(sub, base, [norm_ws(rg.get("title")) or "Reimbursement Guidelines"], counter))

    for tbl in rg.get("tables") or []:
        rows = tbl.get("content") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key, val = norm_ws(row.get("key")), norm_ws(row.get("value"))
            if not (key and val):
                continue
            counter[0] += 1
            units.append(TextUnit(source_type="table", section_path=[norm_ws(rg.get("title")) or "Reimbursement Guidelines", key],
                                  text=f"{key}: {val}", order=counter[0], **base))

    for block in policy.get("Question_Answer_Rules") or []:
        if not isinstance(block, dict):
            continue
        for label, qa in block.items():
            if not isinstance(qa, dict):
                continue
            q, a = norm_ws(qa.get("Q")), norm_ws(qa.get("A"))
            if not a:
                continue
            counter[0] += 1
            units.append(TextUnit(source_type="qa", section_path=[label],
                                  text=a, context=q, order=counter[0], **base))
    return units


# ----------------------------------------------------------------------------
# Sentence segmentation
# ----------------------------------------------------------------------------

_ABBREV = [
    "e.g.", "i.e.", "etc.", "vs.", "approx.", "No.", "Nos.", "Dr.", "Mr.", "Mrs.",
    "Ms.", "Inc.", "Co.", "Ltd.", "U.S.", "U.S.C.", "St.", "Jr.", "Sr.", "Fig.",
    "cf.", "Ph.D.", "M.D.", "a.k.a.", "a/k/a", "ea.", "min.", "max.", "hrs.",
]
_MASKS = {a: f"\x00{i}\x00" for i, a in enumerate(_ABBREV)}
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=["(\u201c]?[A-Z0-9])')
_BULLET = re.compile(r"^\s*(?:[a-zA-Z]\.|\d+\.|[\u2022\u25cf\-\u2013])\s+")


def split_sentences(text: str) -> List[str]:
    if not text:
        return []
    masked = text
    for a, m in _MASKS.items():
        masked = masked.replace(a, m)
    # protect decimals, dollar amounts, and code ranges like $100.00 / 93.5 / 93224-93227
    masked = re.sub(r"(\$?\d+)\.(\d+)", lambda m: m.group(1) + "\x01" + m.group(2), masked)

    parts: List[str] = []
    for chunk in _SENT_SPLIT.split(masked):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)

    out: List[str] = []
    for p in parts:
        for a, m in _MASKS.items():
            p = p.replace(m, a)
        p = p.replace("\x01", ".")
        p = _BULLET.sub("", p).strip()
        if len(p) >= 15:
            out.append(p)
    return out


# ----------------------------------------------------------------------------
# Boilerplate
# ----------------------------------------------------------------------------

BOILERPLATE = [
    r"proprietary information of", r"copyright \d{4}", r"all rights reserved",
    r"is a registered trademark", r"cpt copyright", r"this policy is applicable to",
    r"you are responsible for submission", r"references to cpt or other sources",
    r"this information is intended to serve only", r"may modify this reimbursement policy",
    r"reimbursement policies do not include notations", r"for more information on a specific enrollee",
    r"call the customer service number", r"skip secondary navigation",
    r"policy version change", r"history section: entries prior",
    r"attachments section: updated", r"^policy (implemented|approved)$",
    r"please refer to the", r"^refer to\b", r"^see the\b", r"^resources$",
    r"encourages physicians and other health care professionals to keep current",
    r"sign up for regular distributions", r"^questions and answers$",
    r"^table of contents$", r"^application$", r"^overview$", r"^definitions$",
    r"^history$", r"^attachments$", r"policy does not address all issues",
    r"strives to minimize these variations", r"are included only as a general reference tool",
]
_BOILER_RE = re.compile("|".join(BOILERPLATE), re.I)


def is_boilerplate(sentence: str) -> bool:
    s = sentence.strip()
    if len(s) < 25:
        return True
    if _BOILER_RE.search(s):
        return True
    letters = sum(c.isalpha() for c in s)
    if letters < len(s) * 0.4:          # mostly numbers/punctuation -> table debris
        return True
    if not re.search(r"\b(?:is|are|was|were|will|shall|may|must|should|can|does|do|has|have|applies|utilize|experience|adjust|adjustments?|downcod\w*)\b", s, re.I) \
            and not re.search(r"\b(?:report|bill|submit|reimburse|allow|deny|use|append)\w*\b", s, re.I):
        return True
    return False


# ----------------------------------------------------------------------------
# Codes and modifiers
# ----------------------------------------------------------------------------

CPT_NUM = r"\d{5}"
CPT_CAT3 = r"\d{4}[TU]"
HCPCS = r"[A-VX-Z]\d{4}"
CODE_TOKEN = rf"(?:{CPT_CAT3}|{HCPCS}|{CPT_NUM})"
_CODE_RE = re.compile(rf"\b({CODE_TOKEN})\b")
_RANGE_RE = re.compile(rf"\b({CODE_TOKEN})\s*(?:-|through|to|thru)\s*({CODE_TOKEN})\b", re.I)
_CODE_CONTEXT = re.compile(r"\b(cpt|hcpcs|code|codes|procedure|modifier|category iii|level ii)\b", re.I)

# 5-digit numbers that are almost never CPT codes in this corpus
_CODE_STOP = {"15000", "10000", "20000", "30000", "50000"}


def expand_range(lo: str, hi: str, universe: Optional[set] = None) -> List[str]:
    """Expand 93224-93227, 00100-01999, etc. to their members; keep only plausible codes."""
    m1, m2 = re.fullmatch(r"([A-Z]?)(\d+)([TU]?)", lo), re.fullmatch(r"([A-Z]?)(\d+)([TU]?)", hi)
    if not (m1 and m2) or m1.group(1) != m2.group(1) or m1.group(3) != m2.group(3):
        return [lo, hi]
    a, b = int(m1.group(2)), int(m2.group(2))
    if not (0 <= b - a <= 2500):
        return [lo, hi]
    width = len(m1.group(2))
    out = [f"{m1.group(1)}{n:0{width}d}{m1.group(3)}" for n in range(a, b + 1)]
    if universe and len(out) > 25:
        kept = [c for c in out if c in universe]
        if len(kept) >= 2:
            return kept
    return out


def extract_codes(text: str, universe: Optional[set] = None,
                  expand: bool = True) -> Tuple[List[str], List[str]]:
    """Return (individual codes, literal ranges seen)."""
    codes: List[str] = []
    ranges: List[str] = []
    has_ctx = bool(_CODE_CONTEXT.search(text))

    for m in _RANGE_RE.finditer(text):
        lo, hi = m.group(1), m.group(2)
        ranges.append(f"{lo}-{hi}")
        codes.extend(expand_range(lo, hi, universe) if expand else [lo, hi])

    for m in _CODE_RE.finditer(text):
        tok = m.group(1)
        if tok in _CODE_STOP:
            continue
        if re.fullmatch(CPT_NUM, tok):
            # bare 5-digit: require corroboration to avoid years, ICNs, dollar figures
            if universe and tok in universe:
                pass
            elif not has_ctx:
                continue
        codes.append(tok)

    seen, ordered = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered, sorted(set(ranges))


_MOD_LEAD = re.compile(r"\bmodifier(?:s)?\b|\bappend(?:ed|ing)?\b|\bwith\b|\bappropriate\b", re.I)
_MOD_TOKEN = re.compile(r"\b-?([0-9]{2}|[A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\b")
_MOD_STOP = {"OR", "AND", "IS", "TO", "BE", "IN", "OF", "IF", "AS", "AT", "ON", "BY",
             "NO", "IT", "WE", "SO", "AN", "DO", "US", "MA", "UN", "CMS", "CPT", "QA",
             "ED", "EM", "MD", "RN", "PA", "NP", "IV", "IM", "SC", "ID", "OP", "IP",
             "FL", "DR", "MR", "MS", "HE", "UP", "MY", "OK", "TV"}


def _expand_modifier_ranges(text: str) -> List[str]:
    out = []
    for m in re.finditer(r"\b([A-Z])([0-9])\s*-\s*([A-Z])([0-9])\b", text):
        l1, d1, l2, d2 = m.groups()
        if l1 == l2 and int(d1) <= int(d2):
            for d in range(int(d1), int(d2) + 1):
                out.append(f"{l1}{d}")
    return out


def extract_modifiers(text: str) -> List[str]:
    """Pull modifier values from phrases like 'modifier 52', 'modifiers 25, 59 and 91', 'F1-F9, FA, T1-T9, TA'."""
    found: List[str] = []

    # 1. Expand modifier ranges like F1-F9, T1-T9, E1-E4
    found.extend(_expand_modifier_ranges(text))

    # 2. Check for explicit modifier cues
    for m in re.finditer(r"\bmodifier(?:s)?\b", text, re.I):
        window = text[m.end(): m.end() + 120]
        window = re.split(r"[.;:]|\bwhen\b|\bthat\b|\bwhich\b", window)[0]
        for tok in _MOD_TOKEN.finditer(window):
            val = tok.group(1).upper()
            if val in _MOD_STOP:
                continue
            found.append(val)

    # 3. Direct anatomical & standard modifier pattern matching when text is modifier-relevant
    if re.search(r"\b(modifier|append|with|code|digit|site|anatomical|bilateral|reduced|discontinued)\b", text, re.I):
        for m in re.finditer(r"\b(FA|F[1-9]|TA|T[1-9]|E[1-4]|LC|LD|LM|RC|RI|RT|LT|25|26|TC|50|51|52|53|54|55|56|57|58|59|62|66|73|74|76|77|78|79|80|81|82|AS|XE|XP|XS|XU|PO|QW|CT|FX|FY|JW|JZ|GA|GX|GY|GZ)\b", text):
            val = m.group(1).upper()
            found.append(val)

    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:30]


# ----------------------------------------------------------------------------
# Conditions / limitations / exceptions
# ----------------------------------------------------------------------------

_COND_PATTERNS = [
    r"\bwhen\b[^.;]{6,160}", r"\bif\b[^.;]{6,160}", r"\bunless\b[^.;]{6,160}",
    r"\bonly (?:if|when|once|one)\b[^.;]{0,160}", r"\bprovided that\b[^.;]{6,160}",
    r"\bin order (?:for|to)\b[^.;]{6,160}", r"\bas long as\b[^.;]{6,160}",
    r"\bonce per [a-z ]{2,30}", r"\bper (?:day|month|year|week|calendar month|date of service|member|patient|beneficiary|episode|session|encounter)\b",
    r"\bmaximum of [^.;]{2,80}", r"\blimited to [^.;]{2,120}", r"\bno more than [^.;]{2,80}",
    r"\bup to [^.;]{2,80}", r"\bwithin \d+ (?:calendar )?days?[^.;]{0,60}",
    r"\bmust (?:be|have|include|meet|occur|contain|submit)[^.;]{4,140}",
    r"\brequires? [^.;]{4,140}", r"\bfor the same (?:member|patient|date of service|group|individual)[^.;]{0,80}",
    r"\bby the same (?:group|individual|physician)[^.;]{0,100}",
    r"\bon the same (?:date|day|claim)[^.;]{0,80}",
    r"\bduring the same [^.;]{4,100}",
    r"\bunder the following (?:circumstances|conditions)\b[^.;]{0,120}",
    r"\bconsistent with [^.;]{4,120}",
    r"\b(?:is|are|will be) considered [^.;]{4,120}",
    r"\b(?:is|are) not separately reimbursable\b[^.;]{0,100}",
    r"\b(?:is|are) included in [^.;]{4,120}",
    r"\boutside of the [^.;]{4,100}",
    r"\brendered by [^.;]{4,120}",
    r"\bbilled by [^.;]{4,120}",
    r"\bsubject to [^.;]{4,120}",
    r"\bpayable (?:only|when|if) [^.;]{4,120}",
    r"\bwill not reimburse [^.;]{4,120}",
    r"\bwill reimburse [^.;]{4,120}",
]
_COND_RE = [re.compile(p, re.I) for p in _COND_PATTERNS]

EXCEPTION_CUES = [
    r"does not apply", r"do not apply", r"will not apply", r"is not applied",
    r"\binstead\b", r"\bexcept\b", r"\bexception\b", r"\bunless\b", r"\bhowever\b",
    r"in lieu of", r"rather than", r"\boverride\b", r"is not subject to",
    r"are not subject to", r"this policy does not", r"other than",
]
_EXC_RE = re.compile("|".join(EXCEPTION_CUES), re.I)


def extract_conditions(text: str) -> List[str]:
    out: List[str] = []
    for rx in _COND_RE:
        for m in rx.finditer(text):
            frag = norm_ws(m.group(0)).rstrip(",")
            if len(frag) > 160:                      # cut cleanly, never mid-word
                frag = frag[:160].rsplit(" ", 1)[0] + "..."
            if 8 <= len(frag):
                out.append(frag)
    # de-dup, dropping fragments fully contained in a longer one
    out = sorted(set(out), key=len, reverse=True)
    kept: List[str] = []
    for frag in out:
        if not any(frag.lower() in k.lower() for k in kept):
            kept.append(frag)
    return kept[:8]


def has_exception_cue(text: str) -> bool:
    return bool(_EXC_RE.search(text))


def extract_exception_clauses(text: str) -> List[str]:
    out = []
    for sent in split_sentences(text) or [text]:
        if _EXC_RE.search(sent):
            out.append(norm_ws(sent))
    return out[:5]


# ----------------------------------------------------------------------------
# Attachment (linked PDF) code tables
# ----------------------------------------------------------------------------

_CODE_COL = re.compile(r"^(code|codes|cpt|hcpcs|cpt code|hcpcs code|procedure code|"
                       r"cpt/hcpcs|cpt or hcpcs|hcpcs level ii code|cpt code\(s\))$", re.I)
_LINE_CODE = re.compile(rf"^{CODE_TOKEN}$")


@dataclass
class AttachmentTable:
    url: str
    name: str
    pages: List[int]
    attribute: str                                   # name of the non-code column
    mapping: Dict[str, List[str]] = field(default_factory=dict)
    value_is_code: bool = False                      # code -> code relation table

    @property
    def page(self) -> Optional[int]:
        return self.pages[0] if self.pages else None

    def flat(self) -> Dict[str, str]:
        return {k: ", ".join(v) for k, v in self.mapping.items()}


def _add(mapping: Dict[str, List[str]], code: str, val: str) -> None:
    bucket = mapping.setdefault(code, [])
    if val not in bucket:
        bucket.append(val)


def attachment_code_tables(policy: Dict[str, Any]) -> List[AttachmentTable]:
    """
    Find code -> attribute lookups inside linked PDFs.

    Handles two extractor quirks seen in this corpus:
      * continuation pages lose the header row, so the first data row shows up
        as the column names - those pages are folded into the previous table;
      * a code may map to several values (comprehensive -> component lists).
    """
    tables: List[AttachmentTable] = []
    for item in policy.get("linked_pdf_contents") or []:
        url = item.get("url") or ""
        name = url.rsplit("/", 1)[-1] or "attachment"
        current: Optional[AttachmentTable] = None
        got_table = False

        for tbl in item.get("tables") or []:
            cols = [str(c) for c in (tbl.get("columns") or [])]
            if not (2 <= len(cols) <= 3):
                continue
            page = tbl.get("page")
            code_col = next((c for c in cols if _CODE_COL.match(c.strip())), None)

            if code_col:
                attr = next((c for c in cols if c != code_col), "Value")
                current = AttachmentTable(url, name, [page] if page else [], attr)
                tables.append(current)
                got_table = True
                pairs = [(str(r.get(code_col, "")), str(r.get(attr, "")))
                         for r in tbl.get("rows") or [] if isinstance(r, dict)]
            elif current and re.fullmatch(CODE_TOKEN, cols[0].strip()):
                # headerless continuation: the header cells are really data
                if page and page not in current.pages:
                    current.pages.append(page)
                pairs = [(cols[0], cols[1])]
                pairs += [(str(r.get(cols[0], "")), str(r.get(cols[1], "")))
                          for r in tbl.get("rows") or [] if isinstance(r, dict)]
            else:
                continue

            for raw_code, raw_val in pairs:
                code, val = norm_ws(raw_code), norm_ws(raw_val)
                if re.fullmatch(CODE_TOKEN, code) and val and len(val) < 120:
                    _add(current.mapping, code, val)

        if got_table:
            continue

        # fallback: page text laid out as "Code / Time Span / 0688T / Calendar Month / ..."
        for page in item.get("content") or []:
            if not isinstance(page, dict):
                continue
            lines = [l.strip() for l in str(page.get("content") or "").split("\n") if l.strip()]
            attr = None
            for i, line in enumerate(lines[:6]):
                if _CODE_COL.match(line) and i + 1 < len(lines):
                    attr = lines[i + 1]
                    break
            mapping: Dict[str, List[str]] = {}
            i = 0
            while i < len(lines) - 1:
                if _LINE_CODE.match(lines[i]) and not _LINE_CODE.match(lines[i + 1]) \
                        and len(lines[i + 1]) < 60:
                    _add(mapping, lines[i], lines[i + 1])
                    i += 2
                else:
                    i += 1
            if len(mapping) >= 5:
                tables.append(AttachmentTable(url, name, [page.get("page")] if page.get("page") else [],
                                              attr or "Value", mapping))

    # A long code list repeats its header on every page, so it arrives as one
    # table per page. Fold them back into a single lookup per attachment.
    merged: Dict[Tuple[str, str], AttachmentTable] = {}
    for t in tables:
        key = (t.url, t.attribute.strip().lower())
        if key not in merged:
            merged[key] = t
            continue
        keep = merged[key]
        for code, vals in t.mapping.items():
            for v in vals:
                _add(keep.mapping, code, v)
        for p in t.pages:
            if p and p not in keep.pages:
                keep.pages.append(p)
    tables = list(merged.values())
    for t in tables:
        t.pages.sort()

    for t in tables:
        vals = [v for vs in t.mapping.values() for v in vs]
        t.value_is_code = bool(vals) and sum(bool(re.fullmatch(CODE_TOKEN, v)) for v in vals) > len(vals) * 0.8
    return [t for t in tables if len(t.mapping) >= 3]
