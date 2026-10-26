"""
policy_lib.py
=============
Shared library for the Aetna reimbursement and payment policy rule pipeline.
Handles:
  * Policy loading and structured flattening into addressable TextUnits
  * Syntactic reconstruction of bulleted criteria with deontic lead-ins
  * CPT / HCPCS / modifier extraction with range expansion
  * Section E code table parsing
  * Boilerplate and disclaimer detection
  * PDF page indexing via PyMuPDF (pymupdf)
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
    source_type: str          # guideline | table | qa
    section_path: List[str]   # breadcrumb hierarchy
    text: str
    url: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    context: str = ""         # e.g., prompt, lead-in, or table key
    codes: List[str] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        crumb = " > ".join(self.section_path) if self.section_path else "Reimbursement Guidelines"
        if self.source_type == "qa":
            return f"Questions and Answers > {crumb}"
        if self.source_type == "table":
            return f"Code/Coverage Table > {crumb}"
        return crumb


# ----------------------------------------------------------------------------
# Normalization & Helpers
# ----------------------------------------------------------------------------

_WS = re.compile(r"[ \t\u00a0]+")
_NL = re.compile(r"\s*\n\s*")

PAGE_HEADER_RE = re.compile(
    r"^(?:AETNA\s*BETTER\s*HEALTH|Policy\s*Name:|Page:\s*\d+|Department:|Policy\s*Number:|"
    r"Subsection:|Effective\s*Date:|Applies\s*to:|Medical\s*Payment\s*Policy|Revised:|Reviewed:|"
    r"Proprietary|Kimberly\s*Foltz|Melanie\s*Fernando|Chief\s*Operating\s*Officer|"
    r"Interim\s*Chief\s*Executive\s*Officer|Review/Revision\s*History)",
    re.I
)

BULLET_CHAR_RE = re.compile(r"^[•\u2022\u25cf\-\*\uf0b7\?]+\s*")

IGNORED_TABLE_HEADINGS = {
    "medicaid", "medicare", "international statistical classification", "definition",
    "definitions", "provider", "action", "reviewed", "review/revision history", "resources",
    "codes/condition of coverage", "reimbursement guidelines", "e. codes/condition of coverage",
    "d. reimbursement guidelines", "f. frequently asked questions", "g. review/revision date",
    "h. resources", "e. codes / condition of coverage", "n/a", ""
}


def norm_ws(text: Any) -> str:
    if text is None or text == "":
        return ""
    if isinstance(text, dict):
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
    """Loads extraction_rules.json from given path or current folder."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isabs(path) and not os.path.exists(path):
        candidate = os.path.join(base_dir, path)
        if os.path.exists(candidate):
            path = candidate
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------
# Sentence segmentation
# ----------------------------------------------------------------------------

_ABBREV = [
    "e.g.", "i.e.", "etc.", "vs.", "approx.", "No.", "Nos.", "Dr.", "Mr.", "Mrs.",
    "Ms.", "Inc.", "Co.", "Ltd.", "U.S.", "U.S.C.", "St.", "Jr.", "Sr.", "Fig.",
    "cf.", "Ph.D.", "M.D.", "a.k.a.", "a/k/a", "ea.", "min.", "max.", "hrs.", "pos.",
]
_MASKS = {a: f"\x00{i}\x00" for i, a in enumerate(_ABBREV)}

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'\(•\-])")


def split_sentences(text: str) -> List[str]:
    """Segment policy prose into sentences without splitting abbreviations."""
    if not text:
        return []
    t = text
    for a, mask in _MASKS.items():
        t = re.sub(re.escape(a), mask, t, flags=re.I)

    t = re.sub(r"(?<=\d)\.(?=\d)", "\x00dot\x00", t)
    raw_splits = _SENTENCE_BOUNDARY.split(t)
    out = []
    for s in raw_splits:
        s = s.replace("\x00dot\x00", ".")
        for a, mask in _MASKS.items():
            s = s.replace(mask, a)
        s = norm_ws(s)
        if s and len(s) > 15:
            out.append(s)
    return out


# ----------------------------------------------------------------------------
# Syntactic Reconstruction of Aetna Guidelines
# ----------------------------------------------------------------------------

def clean_guideline_statements(raw_text: str) -> List[Tuple[str, str]]:
    """
    Cleans raw guidelines text, joins soft line-wraps, filters page headers,
    and binds deontic lead-ins with bullet statements.
    Returns list of (full_statement, category_tag) pairs.
    """
    if not raw_text:
        return []

    cleaned = raw_text.replace("\xa0", " ").replace("\u2022", "•")
    # Repair common broken tokens across linebreaks
    cleaned = re.sub(r"HCPCS\s*\n\s*Level\s*II", "HCPCS Level II", cleaned, flags=re.I)
    cleaned = re.sub(r"ICD-10-\s*\n\s*CM", "ICD-10-CM", cleaned, flags=re.I)
    cleaned = re.sub(r"E&M\s*\n\s*CPT", "E&M CPT", cleaned, flags=re.I)
    cleaned = re.sub(r"CPT\s*\n\s*code\(s\)", "CPT code(s)", cleaned, flags=re.I)

    lines = [
        l.strip()
        for l in cleaned.split("\n")
        if l.strip() and not PAGE_HEADER_RE.match(l.strip())
    ]

    statements: List[Tuple[str, str]] = []
    current_lead = ""
    current_lead_kind = "base"
    current_bullet: List[str] = []

    def flush_bullet():
        nonlocal current_bullet
        if current_bullet:
            b_text = BULLET_CHAR_RE.sub("", " ".join(current_bullet)).strip()
            if b_text and len(b_text) > 8:
                if current_lead:
                    full = f"{current_lead.rstrip(': ,')} {b_text}"
                else:
                    full = b_text
                if not full.endswith((".", ";")):
                    full += "."
                tag = current_lead_kind
                if "denied" in full.lower() or "not separately" in full.lower():
                    tag = "denial"
                statements.append((norm_ws(full), tag))
            current_bullet = []

    for l in lines:
        is_bullet = bool(BULLET_CHAR_RE.match(l))
        is_denial_lead = bool(re.search(r"denied\s*(?:when|as)?", l, re.I))
        is_reimb_lead = bool(re.search(r"will\s*(?:only\s*)?reimburse", l, re.I))
        is_reporting_lead = bool(re.search(r"^Appropriate\s*reporting\s*includes", l, re.I))
        is_lead = (
            (is_denial_lead or is_reimb_lead or is_reporting_lead) and
            (bool(re.search(r"(?:when|includes|includes\s*:|for)\s*$", l, re.I)) or len(l) < 120)
        )

        if is_bullet:
            flush_bullet()
            current_bullet.append(BULLET_CHAR_RE.sub("", l))
        elif is_lead and not is_bullet:
            flush_bullet()
            current_lead = l
            current_lead_kind = "denial" if is_denial_lead else "base"
        elif current_bullet:
            current_bullet.append(l)
        else:
            flush_bullet()
            if len(l) > 20:
                tag = "denial" if ("denied" in l.lower() or "not separately" in l.lower()) else "base"
                s = l if l.endswith((".", ";")) else f"{l}."
                statements.append((norm_ws(s), tag))

    flush_bullet()
    return statements


def flatten_policy(policy: Dict[str, Any], idx: int) -> List[TextUnit]:
    """Turn one Aetna policy record into an ordered list of clean TextUnits."""
    base = dict(
        policy_idx=idx,
        policy_title=norm_ws(policy.get("policy_title")) or "Untitled Policy",
        policy_number=norm_ws(policy.get("policy_number")) or None,
        line_of_business=norm_ws(policy.get("Line_of_Business")) or "Illinois Medicaid",
        billing_class=norm_ws(policy.get("Billingclass")) or "Professional",
    )
    counter = [0]
    units: List[TextUnit] = []

    rg = policy.get("reimbursement_guidelines") or {}
    rg_title = norm_ws(rg.get("title")) or "Reimbursement Guidelines"
    content = rg.get("content") or ""

    # 1. Cleaned guideline statements
    guideline_stmts = clean_guideline_statements(content)
    for stmt, tag in guideline_stmts:
        counter[0] += 1
        path = [rg_title, "Coverage Criteria" if tag == "base" else "Denial Criteria"]
        units.append(TextUnit(
            source_type="guideline",
            section_path=path,
            text=stmt,
            order=counter[0],
            **base
        ))

    # 2. Section E / Code tables
    for tbl in rg.get("tables") or []:
        for row in tbl.get("content") or []:
            if not isinstance(row, dict):
                continue
            k = norm_ws(row.get("key"))
            v = norm_ws(row.get("value"))
            if not k:
                continue

            k_clean = k.lower().strip()
            if k_clean in IGNORED_TABLE_HEADINGS:
                continue

            # Skip table dumps duplicating Section D verbatim
            if k.startswith("ABH IL will only reimburse") or "claims that are submitted will be denied" in k.lower():
                continue

            codes, _ = extract_codes(f"{k} {v}")
            # Keep table rows that define codes or substantive coverage rules
            is_substantive = bool(codes) or (len(k) > 30 and bool(v))
            if not is_substantive:
                continue

            counter[0] += 1
            full_text = f"{k}: {v}" if v else k
            units.append(TextUnit(
                source_type="table",
                section_path=[rg_title, "Codes and Specifications"],
                text=full_text,
                order=counter[0],
                codes=codes,
                **base
            ))

    # 3. Question / Answer Rules
    for block in policy.get("Question_Answer_Rules") or []:
        if not isinstance(block, dict):
            continue
        for label, qa in block.items():
            if not isinstance(qa, dict):
                continue
            q, a = norm_ws(qa.get("Q")), norm_ws(qa.get("A"))
            if not a or a == "N/A":
                continue
            counter[0] += 1
            units.append(TextUnit(
                source_type="qa",
                section_path=["Questions and Answers", label],
                text=f"Q: {q} A: {a}" if q else a,
                context=q,
                order=counter[0],
                **base
            ))

    return units


# ----------------------------------------------------------------------------
# Code extraction & range expansion
# ----------------------------------------------------------------------------

CODE_TOKEN_REGEX = re.compile(r"\b(?:\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\b")
RANGE_REGEX = re.compile(
    r"\b(\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\s*(?:-|to|through|thru)\s*(\d{5}|\d{4}[TU]|[A-VX-Z]\d{4})\b",
    re.I
)

CODE_STOP_WORDS = {
    "15000", "10000", "20000", "30000", "50000", "11291", "12020", "11202",
    "112910", "01202", "012020", "08202", "08222", "11302", "12042",
    "2023", "2024", "2025", "2026", "2027", "2028"
}


def expand_range(lo: str, hi: str) -> List[str]:
    """Expand code ranges like 95004-95008 into individual codes."""
    if lo.isdigit() and hi.isdigit() and len(lo) == len(hi) == 5:
        a, b = int(lo), int(hi)
        if 0 <= b - a <= 150:
            return [f"{n:05d}" for n in range(a, b + 1)]
    if len(lo) == 5 and len(hi) == 5 and lo[0].isalpha() and hi[0].isalpha() and lo[0] == hi[0]:
        prefix = lo[0]
        if lo[1:].isdigit() and hi[1:].isdigit():
            a, b = int(lo[1:]), int(hi[1:])
            if 0 <= b - a <= 150:
                return [f"{prefix}{n:04d}" for n in range(a, b + 1)]
    return [lo, hi]


def extract_codes(text: str, universe: Optional[Set[str]] = None) -> Tuple[List[str], List[str]]:
    """Extracts individual CPT/HCPCS codes and raw range strings from text."""
    if not text:
        return [], []

    codes: Set[str] = set()
    ranges: List[str] = []

    for m in RANGE_REGEX.finditer(text):
        lo, hi = m.group(1).upper(), m.group(2).upper()
        ranges.append(f"{lo}-{hi}")
        for c in expand_range(lo, hi):
            if c not in CODE_STOP_WORDS:
                codes.add(c)

    for m in CODE_TOKEN_REGEX.finditer(text):
        tok = m.group(0).upper()
        if tok not in CODE_STOP_WORDS:
            codes.add(tok)

    if universe:
        filtered = {c for c in codes if c in universe}
        if filtered:
            codes = filtered

    return sorted(codes), ranges


# ----------------------------------------------------------------------------
# Modifier extraction
# ----------------------------------------------------------------------------

KNOWN_MODIFIERS = {
    "22", "24", "25", "26", "50", "51", "52", "53", "58", "59", "62", "73", "74",
    "76", "77", "78", "79", "80", "81", "82", "90", "91", "93", "95", "99",
    "AS", "JW", "JZ", "QW", "TC",
    "RT", "LT",
    "E1", "E2", "E3", "E4",
    "FA", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    "TA", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9",
    "LC", "LD", "LM", "RC", "RI",
    "PA", "PB", "PC",
}

MOD_RE = re.compile(
    r"\b(?:modifier[s]?\s*[-–]?\s*([0-9A-Z]{2})|([0-9A-Z]{2})\s*modifier[s]?|-([0-9A-Z]{2})\b)\b",
    re.I
)


def extract_modifiers(text: str) -> List[str]:
    """Finds all clinical modifiers mentioned in text."""
    if not text:
        return []
    mods: Set[str] = set()

    for m in MOD_RE.finditer(text):
        tok = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if tok in KNOWN_MODIFIERS:
            mods.add(tok)

    for mod in ["25", "59", "24", "26", "TC", "JW", "QW", "RT", "LT", "50", "52", "73", "74", "80", "AS"]:
        if re.search(rf"\b(?:modifier\s+)?{mod}\b", text, re.I) and mod in KNOWN_MODIFIERS:
            if f"modifier {mod.lower()}" in text.lower() or f"{mod.lower()} modifier" in text.lower() or f"-{mod.lower()}" in text.lower():
                mods.add(mod)

    return sorted(mods)


# ----------------------------------------------------------------------------
# Conditions & Exceptions
# ----------------------------------------------------------------------------

CONDITION_PATTERNS = [
    re.compile(r"\bwhen\s+([^.;]+)", re.I),
    re.compile(r"\bonly\s+when\s+([^.;]+)", re.I),
    re.compile(r"\bprovided\s+(?:that\s+)?([^.;]+)", re.I),
    re.compile(r"\breported\s+with\s+([^.;]+)", re.I),
    re.compile(r"\bsubmitted\s+with\s+([^.;]+)", re.I),
    re.compile(r"\bno\s+more\s+than\s+([^.;]+)", re.I),
    re.compile(r"\bmaximum\s+(?:of\s+)?([^.;]+)", re.I),
    re.compile(r"\blimited\s+to\s+([^.;]+)", re.I),
    re.compile(r"\bfor\s+patients\s+([^.;]+)", re.I),
    re.compile(r"\bwithin\s+([^.;]+)", re.I),
]

EXCEPTION_PATTERNS = [
    re.compile(r"\b(?:unless|except|with the exception of|in lieu of|instead of)\s+([^.;]+)", re.I),
    re.compile(r"\bmodifier\s+59\s+to\s+indicate\s+([^.;]+)", re.I),
    re.compile(r"\bappended\s+with\s+(?:either\s+)?modifier\s+([^.;]+)", re.I),
    re.compile(r"\bexcluded\s+from\s+unit\s+limits\b", re.I),
]


def extract_conditions(text: str) -> List[str]:
    conds: List[str] = []
    for pat in CONDITION_PATTERNS:
        for m in pat.finditer(text):
            clause = norm_ws(m.group(0))
            if len(clause) > 12 and clause not in conds:
                conds.append(clause)
    return conds[:5]


def extract_exceptions(text: str) -> List[str]:
    excs: List[str] = []
    for pat in EXCEPTION_PATTERNS:
        for m in pat.finditer(text):
            clause = norm_ws(m.group(0))
            if len(clause) > 10 and clause not in excs:
                excs.append(clause)
    return excs[:3]


def is_boilerplate(text: str) -> bool:
    t = text.lower()
    if len(t) < 15:
        return True
    boilerplate_phrases = [
        "proprietary", "all rights reserved", "page 1 of", "page 2 of",
        "department: medical", "effective date:", "revised:", "reviewed:",
        "review/revision history", "legal/contract reference", "statement of objective:",
        "definitions:", "chief executive officer", "chief operations officer"
    ]
    return any(p in t for p in boilerplate_phrases) and len(t) < 120


# ----------------------------------------------------------------------------
# PDF Page Indexer (PyMuPDF)
# ----------------------------------------------------------------------------

class PDFPageIndexer:
    """Indexes source PDF policy pages to resolve exact page numbers."""

    def __init__(self, pdf_dir: str):
        self.pdf_dir = pdf_dir
        self.doc_pages: Dict[str, List[str]] = {}
        self.file_map: Dict[str, str] = {}
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not os.path.isdir(self.pdf_dir):
            return

        manifest_path = os.path.join(self.pdf_dir, "manifest.csv")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        title = norm_ws(row.get("title", "")).lower()
                        fname = row.get("file", "").strip()
                        if title and fname:
                            self.file_map[title] = fname
            except Exception:
                pass

        try:
            import pymupdf
        except ImportError:
            return

        for fname in os.listdir(self.pdf_dir):
            if not fname.lower().endswith(".pdf"):
                continue
            fpath = os.path.join(self.pdf_dir, fname)
            try:
                doc = pymupdf.open(fpath)
                pages = [norm_ws(page.get_text() or "").lower() for page in doc]
                self.doc_pages[fname.lower()] = pages
            except Exception:
                continue

    def resolve_page(self, policy_number: Optional[str], policy_title: str, query_text: str) -> str:
        """Finds 1-indexed page where query_text appears."""
        if not self.doc_pages or not query_text:
            return "1"

        target_file = ""
        t_key = policy_title.lower().strip()
        if t_key in self.file_map:
            target_file = self.file_map[t_key].lower()
        elif f"{t_key} policy" in self.file_map:
            target_file = self.file_map[f"{t_key} policy"].lower()

        if not target_file and policy_number:
            pnum_clean = policy_number.lower().replace("-", "")
            for fname in self.doc_pages:
                if pnum_clean in fname.replace("-", ""):
                    target_file = fname
                    break

        if not target_file:
            clean_t = re.sub(r"[^a-z0-9]", "", policy_title.lower())
            for fname in self.doc_pages:
                clean_f = re.sub(r"[^a-z0-9]", "", fname.lower())
                if clean_t[:15] in clean_f:
                    target_file = fname
                    break

        if not target_file or target_file not in self.doc_pages:
            return "1"

        pages = self.doc_pages[target_file]
        clean_probe = re.sub(r"[^a-z0-9 ]", "", query_text.lower())
        tokens = [t for t in clean_probe.split() if len(t) > 3]

        if not tokens:
            return "1"

        best_page = 1
        best_score = 0
        for pno, ptext in enumerate(pages, 1):
            score = sum(1 for tok in tokens if tok in ptext)
            if score > best_score:
                best_score = score
                best_page = pno

        return str(best_page)
