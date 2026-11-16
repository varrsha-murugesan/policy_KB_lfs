#!/usr/bin/env python3
"""
generate_billing_rules_devoted.py
=================================
Devoted Health deterministic billing-rule generator (Medicare Advantage).

Input : extraction_rules.json   (list of policies with `extracted_content` blocks + `tables`)
Output: billing_rules_devoted.{xlsx,json,csv} + detail / code-map CSVs + queries .sql

Same engine, lexicon, clubbing, workbook layout and one-table SQL as the Humana and UHC
generators (rule_core.py / sql_skeleton.py), so the three payers compare code-by-code.

What is Devoted-specific
  1. The PDF extractor emits ONE BLOCK PER PRINTED LINE. Lines of the same section are joined
     back into sentences before rules are picked, so no rule is cut at a line or page break.
  2. Page headers / footers ("DEVOTED HEALTH FOR PUBLIC USE", "Page 2 of 5", "CLMS.CP.0040 -
     2026-08-20 v2.2", "[CLMS.CP.0040] 2026-08-20") are removed.
  3. The extractor tags any "Term: sentence" line as a `definition`. Those are kept as rule
     text unless they sit in a definitions / references section.
  4. Fake sub-headings (a wrapped line mistaken for a heading, footnote lines) are folded back
     into the text; real sub-headings ("FreeStyle Libre CGM Claim Requirements") scope the rules.
  5. Title page, purpose, applicability, scope, definitions, regulatory references, related
     policies, review / version history and disclaimer sections are not rules and are skipped.
  6. Tables: version-history and glossary tables are skipped; page-split ("(Continued)") tables
     are merged; code lists (revenue code / HCPCS / modifier / ICD-10) are attached once to
     the rule that references them instead of creating a rule per row.
  7. Policy-wide rules that name bill types or revenue codes (facility policies) keep them in
     the rule text; they are not blown up into one rule per code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
for _p in (BASE_DIR, BASE_DIR.parent):
    if (_p / "rule_core.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rule_core import (  # noqa: E402
    BOILERPLATE_RE, PLACEHOLDER_CODE_RE, SKIP_SECTION_RE, split_pieces, CorpusBoilerplate, Rule, clean_text, club_rules,
    export_all, extract_codes, finalize, has_directive, is_code, rule_record, select_rule_chunks,
    summarize_params,
)


import rule_core as _rule_core  # noqa: E402
_rule_core.RULE_OPTS.update({k: True for k in _rule_core.RULE_OPTS})   # Devoted needs every option
import sql_skeleton as _sql_skeleton  # noqa: E402
_sql_skeleton.REQUIRED_DX_POSITIVE = True
from rule_core import KNOWN_MODIFIERS  # noqa: E402

INPUT_JSON = BASE_DIR / "extraction_rules_devoted.json"
OUTPUT_DIR = BASE_DIR
PAYER = "Devoted Health"
LOB_LABEL = "Medicare Advantage"
ID_PREFIX = "DEV"

POLICY_SLUG_MAP = {
    "ABNs and GY Modifiers": "ABN",
    "Acute Admissions and Level of Care": "ACUTE",
    "Acute Hospital Care at Home": "AHCAH",
    "Ambulatory Surgical Centers": "ASC",
    "Billing and Cost Sharing for Dual Eligible Members": "DUAL",
    "Claims based PCP Assignment Policy": "PCP",
    "Common Modifiers": "MOD",
    "Critical Access Hospital CAH Billing": "CAH",
    "Discarded Drugs and Biologics": "JWJZ",
    "Emergency Department Services": "ED",
    "FQHC Services": "FQHC",
    "Medicare Part B and D Vaccines and Vaccine Administration": "VACC",
    "NDC Billing Guidelines": "NDC",
    "Observation Services": "OBS",
    "Rural Health Clinics": "RHC",
    "Telehealth Services": "TELE",
    "Therapy Services and KX Modifier": "KX",
    "TIN Validation": "TIN",
    "Vision Services": "VIS",
}

# ------------------------------------------------------------------------------
# OUTPUT: only billing_rule_<client>.xlsx
# ------------------------------------------------------------------------------
import shutil
import tempfile

CLIENT = "devoted"
RULE_XLSX_NAME = f"billing_rules_{CLIENT}.xlsx"
# folder where compare_rules.py reads its inputs
COMPARE_DIR = Path(r"C:\Users\dell\Documents\Payer Policy\Rule Comparison")


def export_xlsx_only(export_call, out_dir: Path, compare_dir: Optional[Path]) -> List[Path]:
    """Run the normal export (rule_core.export_all) into a scratch folder, keep ONLY the workbook and
    copy it, renamed billing_rule_<client>.xlsx, to out_dir and to the Rule Comparison folder."""
    written: List[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        export_call(Path(tmp))
        books = sorted(Path(tmp).glob("*.xlsx"))
        if len(books) != 1:
            raise RuntimeError(f"expected exactly one workbook from export_all, found {len(books)}")
        for d in [Path(out_dir)] + ([Path(compare_dir)] if compare_dir else []):
            d.mkdir(parents=True, exist_ok=True)
            dest = d / RULE_XLSX_NAME
            shutil.copyfile(books[0], dest)
            written.append(dest)
    return written


MARKER_RE = re.compile(r"^\+?([0-9A-Z]{5})(\*{1,6})?$")



def make_slug(title: str, used: Dict[str, str]) -> str:
    if title in used:
        return used[title]
    slug = POLICY_SLUG_MAP.get(title)
    if not slug:
        t = re.sub(r"^Laboratory\s*[–-]\s*", "LAB ", title)
        words = [w for w in re.split(r"[^A-Za-z0-9]+", t)
                 if w and w.lower() not in ("for", "and", "the", "in", "of", "to", "a", "an", "or", "with", "by")]
        slug = "".join(w[0].upper() for w in words[:5]) or "POL"
    base, n = slug, 2
    while slug in used.values():
        slug = f"{base}{n}"; n += 1
    used[title] = slug
    return slug



# ------------------------------------------------------------------------------
# Devoted-specific cleaning
# ------------------------------------------------------------------------------

# page headers / footers the extractor leaves between the lines of a section
NOISE_LINE_RE = re.compile(
    r"^(?:DEVOTED HEALTH FOR PUBLIC USE|\|+|Page \d+ of \d+|"
    r"[A-Z][A-Z0-9.\-]*\s*-\s*\d{4}-\d{2}-\d{2}\s*v\d+(?:\.\d+)?|"
    r"\[[A-Za-z0-9.\-]+\]\s*\d{4}-\d{2}-\d{2})$", re.I)
NOISE_INLINE_RE = re.compile(
    r"DEVOTED HEALTH FOR PUBLIC USE|Page \d+ of \d+|\b[A-Z][A-Z0-9.\-]*\s*-\s*\d{4}-\d{2}-\d{2}\s*v\d+(?:\.\d+)?|"
    r"\[[A-Za-z0-9.\-]+\]\s*\d{4}-\d{2}-\d{2}|_{4,}", re.I)

# sections that never state a billing rule
DEVOTED_SKIP_RE = re.compile(
    r"^(?:acronyms and definitions|regulatory references?|review and version history|policy approval and history|"
    r"related policies.*|applicability|scope|n/a|review|responsibilities|icd-10 cm|\d.*)$", re.I)

ICD_RE_ROW = re.compile(r"^[A-TV-Z]\d{2}(?:\.[0-9A-Z]{1,4})?$")


def section_skipped(path: str) -> bool:
    root = (path or "").split(">")[0].strip()
    leaf = (path or "").split(">")[-1].strip()
    for x in (root, leaf):
        if SKIP_SECTION_RE.match(x):
            return True
    if DEVOTED_SKIP_RE.match(root):
        return True
    low = root.lower()
    if "medicaid" in low and "medicare" not in low:
        return True
    return False


def _real_heading(text: str) -> bool:
    """A second-level hierarchy entry that is a true sub-heading, not a wrapped line / footnote."""
    t = (text or "").strip()
    if not t or t[0].isdigit() or not t[0].isupper() or t[-1] in '.,;:"\')?!' or len(t.split()) > 12:
        return False
    return not re.search(r"\d$", t)


def _cell(v: Any) -> str:
    return clean_text(NOISE_INLINE_RE.sub(" ", str(v or "")))


KNOWN_ROOTS: set = set()      # every top-level heading seen in the corpus (filled in run_pipeline)


def _table_from_item(b: Dict[str, Any], path: str, idx: int) -> Optional[Dict[str, Any]]:
    td = b.get("table_data")
    if not isinstance(td, dict):
        return None
    cols = [_cell(c) for c in (td.get("columns") or [])]
    rows = [{_cell(k): _cell(v) for k, v in (r or {}).items()} for r in (td.get("rows") or [])]
    title = clean_text(td.get("title") or b.get("title") or "")
    header = "" if re.match(r"^Table \(Page \d+\)", title) else re.sub(r"\s*\(Continued\)", "", title).strip()
    cl = [c.lower() for c in cols]
    # version / revision history and glossary tables are not rules
    if ("date" in cl and any("version" in c or "revision" in c for c in cl)) or cl[:2] == ["term", "definition"]:
        return None
    return {"key": (path, tuple(cols)), "path": path, "header": header, "columns": cols, "rows": rows,
            "order": idx, "page": b.get("page_number") or b.get("page_start"), "last_idx": idx,
            "continued": "continued" in title.lower()}


def _merge_split_rows(t: Dict[str, Any]) -> None:
    """A table row whose first cell is empty is the wrapped second half of the row above it."""
    out: List[Dict[str, str]] = []
    for r in t["rows"]:
        first = (list(r.values()) or [""])[0]
        if out and not first and any(r.values()):
            for k, v in r.items():
                if v:
                    out[-1][k] = (out[-1].get(k, "") + " " + v).strip()
        elif any(r.values()):
            out.append(dict(r))
    t["rows"] = out
    t["texts"] = [" – ".join(v for v in r.values() if v) for r in out]


def _join_overlap(cell: str, line: str) -> str:
    """'... was performed the' + 'performed the day after, and during, the' -> no repeated words."""
    import difflib
    cw, lw = cell.split(), line.split()

    def same(a: str, b: str) -> bool:
        a, b = a.lower(), b.lower()
        return a == b or (len(a) > 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)   # tolerate source typos

    for n in range(min(len(cw), len(lw), 8), 0, -1):
        if n >= 3 or n == len(lw):
            if all(same(a, b) for a, b in zip(cw[-n:], lw[:n])):
                return " ".join(cw + lw[n:])
        elif [w.lower() for w in cw[-n:]] == [w.lower() for w in lw[:n]]:
            return " ".join(cw + lw[n:])
    return cell + " " + line


EXTRA_MODIFIERS = {"JK", "JL"}                     # printed as entries in this policy but missing from the shared list
_INSTR_RE = re.compile(r"^(?:do not |don't )?(?:report|use|include|indicate|attest|follow)\b|^modifiers? \w\w should\b|"
                       r"^when\b|^both\b", re.I)


def _is_mod_marker(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{2}", text)) and (text in KNOWN_MODIFIERS or text in EXTRA_MODIFIERS)


class ModAsm:
    """Rebuild 'one modifier = one entry' from a modifier table the PDF printed column by column and
    split over a page break: descriptions / instructions of different modifiers arrive interleaved,
    markers can be missing and a cell can continue after another modifier's text."""

    def __init__(self, root: str):
        self.root, self.entries, self.cur, self.tail = root, {}, None, False

    def _txt(self, mod: str) -> str:
        return " ".join(self.entries[mod]["frags"])

    def _incomplete(self, mod: str) -> bool:
        t = self._txt(mod).strip()
        return t.count("(") > t.count(")") or not re.search(r"[.!?]$", t)

    def start(self, mod: str, idx: int, page: Any, desc: str = "") -> None:
        e = self.entries.setdefault(mod, {"frags": [], "order": idx, "page": page, "waiting": False})
        e["frags"] += [mod] if not e["frags"] else []
        if desc:
            e["frags"].append(desc)
        else:
            e["waiting"] = len(e["frags"]) == 1
        self.cur, self.tail = mod, False

    def add(self, text: str, idx: int, page: Any, upcoming: List[str]) -> None:
        cur = self.cur
        first_alpha = text[0]
        closes_paren = ")" in text and ("(" not in text or text.index(")") < text.index("("))
        continuation = first_alpha.islower() or closes_paren
        if cur and continuation and not self._incomplete(cur):
            # a cell that continues after another modifier's text: give it back to the open entry
            owner = next((m for m in reversed(list(self.entries)) if m != cur and len(self.entries[m]["frags"]) > 1
                          and self._incomplete(m)), None)
            if owner:
                self.cur, self.tail, cur = owner, True, owner
                e = self.entries[owner]
                if closes_paren and self._txt(owner).count("(") > self._txt(owner).count(")"):
                    pos = next((i for i, f in enumerate(e["frags"]) if i and _INSTR_RE.match(f)), len(e["frags"]))
                    e["frags"].insert(pos, text)          # closes the description, before the instructions
                    return
        elif cur and self.tail and first_alpha.isupper() and not _INSTR_RE.match(text) \
                and not re.search(rf"\b{re.escape(cur)}\b", text):
            self.cur, self.tail, cur = None, False, None   # tail of the continued entry is over
        if cur and not continuation and first_alpha.isupper() and not self._incomplete(cur) and not self.tail \
                and not _INSTR_RE.match(text):
            cur = self.cur = None                          # a new entry whose marker was lost
        # the marker row of a table was printed, but its text comes much later: instructions that name
        # another modifier first ("Report modifier RT to ...") belong to that modifier
        m_instr = re.match(r"(?:do not )?report modifier ([A-Z0-9]{2})\b(?! with\b)", text, re.I)
        if cur and self.entries[cur]["waiting"] and m_instr and m_instr.group(1) in self.entries \
                and m_instr.group(1) != cur and not continuation:
            self.cur, self.tail, cur = m_instr.group(1), False, m_instr.group(1)
        if cur is None:
            owner = None
            for nxt in upcoming[:3]:
                m = re.search(r"\b(?:report )?modifier ([A-Z0-9]{2})\b", nxt, re.I)
                if m and _INSTR_RE.match(nxt) and (m.group(1) in KNOWN_MODIFIERS or m.group(1) in EXTRA_MODIFIERS):
                    owner = m.group(1)
                    break
            if owner is None:
                owner = next((k for k, v in self.entries.items() if v["waiting"]), None)
            if owner is None:
                return
            self.entries.setdefault(owner, {"frags": [owner], "order": idx, "page": page, "waiting": False})
            self.cur = cur = owner
        e = self.entries[cur]
        fr = e["frags"]
        if first_alpha.islower() and len(fr) > 2 and re.search(r"[;.:)]$", fr[-1]):
            # a lowercase line that finishes a sentence left hanging earlier in the cell (page-split cell);
            # "however, ..." / "and ..." follow the last line instead
            semi_ok = not re.match(r"(?:however|and|or|but|which|then|see)\b", text, re.I)
            end_re = r"[.!?:)]$" if semi_ok else r"[;.!?:)]$"
            k = next((i for i in range(1, len(fr) - 1) if not re.search(end_re, fr[i]) and fr[i + 1][:1].isupper()), None)
            if k is not None:
                fr.insert(k + 1, text)
                e["waiting"] = False
                return
        fr.append(text)
        e["waiting"] = False

    def sections(self) -> List[Dict[str, Any]]:
        out = []
        for mod, e in self.entries.items():
            txt = " ".join(e["frags"])
            if len(e["frags"]) > 1:
                out.append({"path": f"{self.root} > Modifier {mod}", "text": txt, "order": e["order"],
                            "last_idx": e["order"], "page": e["page"], "modifier": mod})
        return out


def stitch_policy(policy: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (sections, tables) in document order. One extractor block == one printed line, so
    lines of a section are joined back into running text."""
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    items = policy.get("extracted_content") or []
    real_subs = {clean_text(x["hierarchy_path"][1]) for x in items
                 if x.get("content_type") == "header" and len(x.get("hierarchy_path") or []) == 2
                 and _real_heading(clean_text(x["hierarchy_path"][1]))
                 and clean_text(x.get("content")) == clean_text(x["hierarchy_path"][1])}
    # policies that print a modifier table as lines ("52" / "Reduced services" / instructions ...)
    n_mod_lines = sum(1 for x in items if x.get("content_type") == "paragraph"
                      and _is_mod_marker((x.get("content") or "").strip()))
    mod_entries = n_mod_lines >= 3
    asm: Optional[ModAsm] = None
    plain = [clean_text(NOISE_INLINE_RE.sub(" ", (x.get("content") or "").strip())) for x in items]
    plain = [("" if (x.get("content_type") == "table" or NOISE_LINE_RE.match((x.get("content") or "").replace("\n", " ").strip()))
              else p) for x, p in zip(items, plain)]
    override: Optional[Tuple[str, str]] = None
    cur_mod: Optional[str] = None
    cur_sub: Optional[Tuple[str, str]] = None      # (root, heading) of a heading the extractor printed as plain text
    for idx, b in enumerate(items):
        ctype = b.get("content_type") or "paragraph"
        hp = [clean_text(x) for x in (b.get("hierarchy_path") or [])] or [clean_text(b.get("section") or "")]
        root = hp[0]
        if override:
            if root == override[0]:
                root = override[1]
            else:
                override = None
        content = (b.get("content") or "").strip()
        ccl = clean_text(content)
        page = b.get("page_number") or b.get("page_start")
        if ctype == "table" and asm is not None:
            tt = _table_from_item(b, root, idx)
            if tt is not None:
                if _is_mod_marker(tt["columns"][0].strip()):
                    asm.start(tt["columns"][0].strip(), idx, page)       # the header row is really the first marker
                for r in tt["rows"]:
                    vals = [str(v).strip() for v in r.values()]
                    if vals and _is_mod_marker(vals[0]):
                        asm.start(vals[0], idx, page, " ".join(v for v in vals[1:] if v))
            continue
        if ctype == "table":
            cur_mod = None
            t = _table_from_item(b, root, idx)
            if t is None or (section_skipped(root) and not ICD_RE_ROW.match(t["columns"][0].strip())):
                continue            # (an ICD-10 list under an "ICD-10 CM" heading is still the rule's diagnosis list)
            prev = tables[-1] if tables else None
            if prev and prev["key"] == t["key"] and (t["continued"] or idx - prev["last_idx"] <= 2):
                prev["rows"].extend(t["rows"]); prev["last_idx"] = idx
                t = prev
            else:
                tables.append(t)
            last = t["rows"][-1] if t["rows"] else None
            if last and any("modifier" in c.lower() for c in t["columns"][:1]):
                vals = list(last.values())
                if re.fullmatch(r"[A-Z0-9]{2}", vals[0]) and not any(vals[1:]):
                    cur_mod = vals[0]               # the entry's text follows the table as plain lines
                    t["rows"].pop()
            continue
        if NOISE_LINE_RE.match(content.replace("\n", " ").strip()):
            if sections and sections[-1]["last_idx"] == idx - 1:
                sections[-1]["last_idx"] = idx      # page break: keep the section running across it
            if tables and tables[-1]["last_idx"] == idx - 1:
                tables[-1]["last_idx"] = idx
            continue
        # a section heading the extractor printed as a plain paragraph ("REGULATORY REFERENCE", "N/A")
        if ccl in KNOWN_ROOTS and ccl != root and (ccl.isupper() or ccl == "N/A"):
            override, cur_mod = (hp[0], ccl), None
            continue
        if ctype == "header" and ccl == hp[0]:
            cur_mod = None
            continue
        sub = hp[1] if len(hp) > 1 else ""
        path = f"{root} > {sub}" if sub in real_subs else root
        if ctype == "header" or (cur_sub and cur_sub[0] != path.split(" > ")[0]):
            cur_sub = None
        if cur_sub:
            path = f"{path} > {cur_sub[1]}"
        if ctype == "header" and ccl == sub and sub in real_subs:
            cur_mod = None
            continue
        text = clean_text(NOISE_INLINE_RE.sub(" ", content)).replace("○", "◦")
        if not text:
            continue
        # a short sub-heading printed as a plain paragraph between two sentences ("Claim submission and reimbursement")
        if asm is None and ctype == "paragraph" and len(text.split()) <= 8 and text[0].isupper() \
                and text.upper() not in ("OR", "AND", "AND/OR", "N/A") \
                and not re.search(r"[.;:,)?!]$", text) and not cur_mod and idx + 1 < len(items):
            prv = sections[-1] if sections else None
            nxt = plain[idx + 1]
            if prv and prv["last_idx"] == idx - 1 and re.search(r"[.;:?!)]$", prv["text"]) \
                    and nxt and (nxt[0].isupper() or nxt[0] in "•●▪◦-"):
                cur_sub = (path.split(" > ")[0], text)
                continue
        # text printed right after a table that continues its last cell ("performed the day after ...")
        if tables and tables[-1]["last_idx"] == idx - 1 and text[0].islower() and tables[-1]["rows"]:
            row = tables[-1]["rows"][-1]
            k = next((c for c in reversed(list(row)) if row[c]), None)
            if k:
                row[k] = _join_overlap(row[k], text)
                tables[-1]["last_idx"] = idx
                continue
        mod = None
        if mod_entries and ctype == "paragraph" and _is_mod_marker(text):
            cur_mod = None
            if asm is None:
                asm = ModAsm(root)
            asm.start(text, idx, page)
            continue
        if asm is not None and ctype in ("paragraph", "bullet_point", "definition"):
            upcoming = [plain[j] for j in range(idx + 1, min(idx + 12, len(items))) if plain[j]][:3]
            asm.add(text, idx, page, upcoming)
            continue
        if cur_mod and ctype in ("paragraph", "bullet_point", "definition"):
            path, mod = f"{root} > Modifier {cur_mod}", cur_mod
        else:
            cur_mod = None
        if section_skipped(path):
            continue
        prev = sections[-1] if sections else None
        if prev and prev["last_idx"] == idx - 1 and prev["path"] == path:
            glue = "" if (re.search(r"https?://\S*$", prev["text"]) and " " not in text) else " "
            prev["text"] += glue + text
            prev["last_idx"] = idx
        else:
            sections.append({"path": path, "text": text, "order": idx, "last_idx": idx, "page": page,
                             **({"modifier": mod} if mod else {})})
    if asm is not None:
        sections.extend(asm.sections())
        sections.sort(key=lambda z: z["order"])
    for t in tables:
        _merge_split_rows(t)
    return sections, tables


def classify_table(t: Dict[str, Any]) -> str:
    cols_l = [c.lower() for c in t["columns"]]
    header_l = t["header"].lower()
    texts = t["texts"]
    n = max(len(texts), 1)
    placeholder_rows = sum(1 for x in texts if PLACEHOLDER_CODE_RE.search(x) or "$" in x)
    real_code_rows = sum(1 for r in t["rows"] if any(extract_codes(v) for v in r.values()))
    if cols_l and ICD_RE_ROW.match(t["columns"][0].strip()):
        return "ICD_LIST"       # header row was actually data: ICD-10 code + description
    if cols_l and "revenue" in cols_l[0] and len(cols_l) <= 3:
        return "REVENUE_LIST"
    if "example" in header_l or (placeholder_rows / n >= 0.5 and real_code_rows / n < 0.5):
        return "EXAMPLE"
    if any("contractor" in c for c in cols_l) or any(c == "policy number" for c in cols_l):
        return "LCD_REFERENCE"
    if any(is_code(c.rstrip("*")) for c in t["columns"]):
        return "CODE_LIST"  # header row was actually data
    code_cols = [c for c in t["columns"] if _is_code_col(c, t)]
    if any("modifier" in c for c in cols_l) and not code_cols:
        return "MODIFIER"
    if code_cols:
        others = [c for c in t["columns"] if c not in code_cols]
        if all(re.search(r"descri|name", o, re.I) for o in others):
            return "CODE_LIST"
        return "ATTRIBUTE"
    return "TEXT"


def _is_code_col(col: str, t: Dict[str, Any]) -> bool:
    vals = [r.get(col, "") for r in t["rows"]]
    hits = sum(1 for v in vals if extract_codes(v))
    return hits >= max(1, 0.6 * len(vals)) and not re.search(r"descri", col, re.I)


def codes_with_markers(value: str) -> List[Tuple[str, str]]:
    out = []
    for tok in re.split(r"[\s,;/]+", value or ""):
        m = MARKER_RE.match(tok.strip())
        if m and is_code(m.group(1)):
            out.append((m.group(1), m.group(2) or ""))
    return out


# ------------------------------------------------------------------------------
# post-processing fixes (Devoted only; shared rule_core / sql_skeleton are untouched)
# ------------------------------------------------------------------------------
import datetime as _dt


def normalize_date(v: str) -> str:
    """'2/28/26', '5/22/2025' and '2026-09-22' -> '2026-09-22' (one format for every policy)."""
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return v


def icd_for_sql(code: str) -> str:
    """Table-attached ICD-10 codes follow the same dot convention as codes parsed from rule text."""
    return code if _sql_skeleton.ICD_STORED_WITH_DOT else code.replace(".", "")


_MODTOK = r"(?:[A-Z0-9]{2}|X\{EPSU\})"
_NOT_REPORT_LIST_RE = re.compile(    # modifier tokens are upper-case; only the wording is case-insensitive
    rf"(?i:do not report (?:both )?(?:the )?modifiers?)\s+({_MODTOK}(?:\s*(?:,|/|(?i:or|and))\s*(?:(?i:an?)\s+)?(?:{_MODTOK}|(?i:modifier)))*)")
_NOT_REPORT_WITH_RE = re.compile(
    rf"(?i:do not report with (?:the )?modifiers?)\s+({_MODTOK}(?:\s*(?:,|/|(?i:or|and))\s*{_MODTOK})*)")
_KEY_NOT_ALLOWED_RE = re.compile(r"informational only|not recognized|does not recognize|do not recognize|not reimburs|"
                                 r"not allowed|no modifiers?|invalid", re.I)


def fix_modifier_polarity(r: Rule) -> None:
    """A modifier-entry rule defines ONE modifier (the key). 'Do not report modifiers 81, 82 or AS with
    modifier 80' prohibits the OTHER modifiers; it never prohibits the key itself, and a companion that
    the text says not to combine ('instead of 59', 'both 59 and XE') is not required."""
    key = r.extra.get("modifier_key")
    if not (r.extra.get("modifier_row") and key):
        return
    companions: List[str] = []
    for sent in re.split(r"(?<=[.;:])\s+", r.text):
        for rx in (_NOT_REPORT_LIST_RE, _NOT_REPORT_WITH_RE):
            for m in rx.finditer(sent):
                for tok in re.findall(_MODTOK, m.group(1)):
                    companions += ["XE", "XP", "XS", "XU"] if tok.startswith("X{") else [tok]
    companions = [c for c in dict.fromkeys(companions) if c != key and c in r.modifiers]
    prohibited = set(r.mods_prohibited) | set(companions)
    if key in prohibited and not _KEY_NOT_ALLOWED_RE.search(r.text):
        prohibited.discard(key)
    r.mods_prohibited = sorted(prohibited)
    r.mods_required = [m for m in r.mods_required if m not in prohibited]
    if companions:
        # the key modifier is the TRIGGER of a "do not combine" rule, not something every line must carry
        r.extra["mod_trigger"] = key
        r.mods_required = [m for m in r.mods_required if m != key]


_NOT_ALLOWED_LINE_RE = re.compile(r"^(?P<ind>\s*)AND \((?P<body>c\.modifier_1 IN [^\n]*?)\)  -- modifier not allowed$", re.M)


def add_trigger_guard(rec: Dict[str, Any], r: Rule) -> None:
    """'Do not report 81, 82 or AS with modifier 80' may only flag a line that ALSO carries 80."""
    key = r.extra.get("mod_trigger")
    if not key:
        return
    m = _NOT_ALLOWED_LINE_RE.search(rec["SQL_Query"])
    if not m:
        return
    cols = sorted(set(re.findall(r"c\.modifier_(\d)", m.group("body"))))
    trig = " OR ".join(f"c.modifier_{i} IN ('{key}')" for i in cols)
    rec["SQL_Query"] = rec["SQL_Query"][:m.start()] + f"{m.group('ind')}AND ({trig})  -- modifier {key} is on the line\n" \
        + rec["SQL_Query"][m.start():]
    comp = ", ".join(r.mods_prohibited)
    rec["Incorrect_Billing_Condition"] = (f"Line billed with modifier {key} together with modifier(s) {comp}, "
                                          f"which the rule does not allow in combination.")


# ------------------------------------------------------------------------------
# main pipeline
# ------------------------------------------------------------------------------


def run_pipeline(input_json: Path, out_dir: Path, compare_dir: Optional[Path]) -> None:
    print("=" * 80)
    print("DEVOTED HEALTH BILLING RULE GENERATION (Medicare Advantage)")
    print(f"Input: {input_json}")
    print("=" * 80)
    policies = json.loads(Path(input_json).read_text(encoding="utf-8"))
    KNOWN_ROOTS.clear()
    KNOWN_ROOTS.update(clean_text(x["hierarchy_path"][0]) for p in policies for x in (p.get("extracted_content") or [])
                       if x.get("content_type") == "header" and len(x.get("hierarchy_path") or []) == 1
                       and clean_text(x.get("content")) == clean_text(x["hierarchy_path"][0]))
    print(f"Loaded {len(policies)} policy objects.")

    ma_policies, excluded = [], []
    for p in policies:
        lob = p.get("line_of_business") or ""
        (ma_policies if "medicare" in lob.lower() else excluded).append(p)

    # corpus boilerplate detection over all Medicare policies
    boiler = CorpusBoilerplate()
    stitched = []
    for p in ma_policies:
        secs, tabs = stitch_policy(p)
        stitched.append((p, secs, tabs))
        boiler.add_policy(s["text"] for s in secs)

    all_rules: List[Rule] = []
    policy_code_list: List[Dict[str, Any]] = []
    skipped_example_tables = 0
    slugs: Dict[str, str] = {}

    for pol_idx, (p, sections, tables) in enumerate(stitched, 1):
        title = clean_text(p.get("policy_title")) or f"Policy_{pol_idx}"
        pnum = clean_text(p.get("policy_number"))
        pkey = f"{title}|{pnum}"
        slug = make_slug(title, slugs)
        bclass = p.get("billing_class") or "Both"
        common = dict(payer=PAYER, policy_key=pkey, policy_title=title, policy_number=pnum,
                      line_of_business=LOB_LABEL, billing_class=bclass,
                      effective_date=normalize_date(clean_text(p.get("effective_date"))), source_pdf=p.get("source_pdf") or "")
        rules: List[Rule] = []
        claimed: set = set()

        # ---- 1. prose sections ------------------------------------------------
        for sec in sections:
            # a modifier entry (marker + description + instructions) is one self-contained rule
            chunks = [sec["text"]] if sec.get("modifier") else \
                select_rule_chunks(sec["text"], boiler=boiler, claimed=claimed, skip_claimed=False,
                                  whole_max=1500, chunk_max=1500)
            for i, chunk in enumerate(chunks):
                stype = "bullet" if re.match(r"^[•▪◦●–*]", chunk) else "prose"
                r = Rule(source_section=sec["path"], source_type=stype, text=chunk,
                         order=sec["order"] + i / 100.0, source_page=sec["page"], **common)
                if sec.get("modifier"):
                    r.extra["modifier_key"] = sec["modifier"]
                    r.extra["modifier_row"] = True
                    r.add_modifiers([sec["modifier"]])
                r.add_codes(extract_codes(chunk), "rule text")
                rules.append(r)

        # ---- 2. tables ----------------------------------------------------------
        def anchor_for(t: Dict[str, Any], hints: List[str]) -> Optional[Rule]:
            same = [r for r in rules if (r.source_section == t["path"] or r.source_section.startswith(t["path"] + " >"))
                    and r.source_type in ("prose", "bullet")]
            if same:
                before = [r for r in same if r.order <= t["order"]]
                return (before or same)[-1]
            for h in hints:
                hit = [r for r in rules if h in r.text.lower()]
                if hit:
                    return hit[0]
            return None

        for t in tables:
            kind = classify_table(t)
            label = f"Table: {t['header'] or t['path']}"
            if kind == "EXAMPLE":
                skipped_example_tables += 1
                continue

            if kind == "REVENUE_LIST":
                anchor = anchor_for(t, ["revenue code"])
                for row in t["rows"]:
                    vals = list(row.values())
                    if vals and re.fullmatch(r"\d{3,4}X?", vals[0].strip()):
                        policy_code_list.append({"Policy_Title": title, "Table": "Revenue codes " + (t["header"] or t["path"]),
                                                 "Code": vals[0].strip(), "Code_Description": vals[1] if len(vals) > 1 else "",
                                                 "Footnote_Marker": "", "Linked_Rule_ID": "", "_rule": anchor})
                if anchor is not None and label not in anchor.attachments:
                    anchor.attachments.append(label)
                continue

            if kind == "ICD_LIST":
                icd = [x for r in ([dict(zip(t["columns"], t["columns"]))] + t["rows"])
                       for x in [list(r.values())[0].strip()] if ICD_RE_ROW.match(x)]
                listed_re = re.compile(r"(?:diagnos[a-z]*|icd-?10)[^.]{0,60}\b(?:below|following|listed|attached)\b|"
                                       r"\b(?:below|following|listed)\b[^.]{0,60}(?:diagnos[a-z]*|icd-?10)", re.I)
                anchor = next((r for r in rules if listed_re.search(r.text)), None) \
                    or anchor_for({"path": t["path"], "order": t["order"]}, ["icd-10", "diagnos"]) \
                    or anchor_for({"path": "__none__", "order": t["order"]}, ["icd-10", "diagnos"])
                for x in icd:
                    policy_code_list.append({"Policy_Title": title, "Table": "ICD-10 CM " + (t["header"] or t["path"]),
                                             "Code": x, "Code_Description": next(
                                                 (list(r.values())[1] for r in t["rows"] if list(r.values())[0] == x), ""),
                                             "Footnote_Marker": "", "Linked_Rule_ID": "", "_rule": anchor})
                if anchor is not None:
                    anchor.extra.setdefault("icd_codes", [])
                    anchor.extra["icd_codes"] += [icd_for_sql(x) for x in icd if icd_for_sql(x) not in anchor.extra["icd_codes"]]
                    if label not in anchor.attachments:
                        anchor.attachments.append(label)
                continue

            if kind in ("CODE_LIST",):
                rows = list(t["rows"])
                if any(is_code(c.rstrip("*")) for c in t["columns"]):
                    rows = [dict(zip(t["columns"], t["columns"]))] + rows
                before = [r for r in rules if r.order <= t["order"]]
                lead_in = before[-1] if before and before[-1].text.rstrip().endswith(":") else None
                if lead_in is not None:
                    # "... IS REIMBURSABLE at a frequency of one test every three months:" + table
                    anchor = lead_in
                    if len(t["texts"]) <= 40:
                        anchor.text = anchor.text.rstrip() + " " + "; ".join(x for x in t["texts"] if x)
                else:
                    anchor = anchor_for(t, ["following codes", "codes listed", "applicable procedure", "table below"])
                    # only attach a code list to a rule that does not already name its own codes
                    if anchor is not None and anchor.codes:
                        anchor = None
                for row in rows:
                    vals = list(row.values())
                    desc = next((v for k, v in row.items() if re.search(r"descri|name", k, re.I)), "")
                    if not desc and len(vals) > 1 and not codes_with_markers(vals[1]):
                        desc = vals[1]
                    for col_val in vals:
                        for code, marker in codes_with_markers(col_val):
                            policy_code_list.append({
                                "Policy_Title": title, "Table": t["header"] or t["path"], "Code": code,
                                "Code_Description": desc, "Footnote_Marker": marker,
                                "Linked_Rule_ID": "", "_rule": anchor,
                            })
                            if anchor is not None:
                                anchor.add_codes([code], label)
                if anchor is not None and label not in anchor.attachments:
                    anchor.attachments.append(label)
                continue

            if kind == "LCD_REFERENCE":
                anchor = anchor_for({"path": "__none__", "order": t["order"]},
                                    ["local coverage determination", "lcd", "national coverage determination"])
                code_col = next((c for c in t["columns"] if "code" in c.lower()), None)
                for row in t["rows"]:
                    lcd = " – ".join(x for x in (row.get(next((c for c in t["columns"] if "number" in c.lower()), ""), ""),
                                                 row.get(next((c for c in t["columns"] if "title" in c.lower()), ""), ""),
                                                 row.get(next((c for c in t["columns"] if "contractor" in c.lower()), ""), ""))
                                     if x)
                    for code in extract_codes(row.get(code_col, "") if code_col else " ".join(row.values())):
                        if anchor is not None:
                            anchor.code_params.append({"code": code, "relationship": "", "related_code": "",
                                                       "parameter_name": "Original Medicare LCD/NCD",
                                                       "parameter_value": lcd, "source": label})
                            anchor.add_codes([code], label)
                        else:
                            policy_code_list.append({"Policy_Title": title, "Table": t["header"], "Code": code,
                                                     "Code_Description": lcd, "Footnote_Marker": "",
                                                     "Linked_Rule_ID": "", "_rule": None})
                if anchor is not None and label not in anchor.attachments:
                    anchor.attachments.append(label)
                continue

            if kind == "MODIFIER":
                mod_col = next(c for c in t["columns"] if "modifier" in c.lower())
                long_rows = [x for x in t["texts"] if len(x) > 80 and has_directive(x)]
                if long_rows:
                    # each modifier row carries its own instructions -> distinct rule per modifier
                    for j, (row, txt) in enumerate(zip(t["rows"], t["texts"])):
                        mod = (row.get(mod_col) or "").strip().upper()
                        if not txt or not has_directive(txt):
                            continue
                        r = Rule(source_section=f"{t['path']} > {t['header']}".strip(" >"), source_type="table",
                                 text=txt, order=t["order"] + j / 1000.0, source_page=t["page"], **common)
                        r.extra["modifier_key"] = mod
                        r.extra["modifier_row"] = True
                        if re.fullmatch(r"[A-Z0-9]{2}", mod):
                            r.add_modifiers([mod])
                        r.add_codes(extract_codes(txt), "rule text")
                        rules.append(r)
                else:
                    anchor = anchor_for(t, ["modifier"])
                    if anchor is None:
                        anchor = Rule(source_section=t["path"], source_type="table_only",
                                      text=" | ".join(t["texts"]), order=t["order"], source_page=t["page"], **common)
                        rules.append(anchor)
                    other = [c for c in t["columns"] if c != mod_col]
                    for row in t["rows"]:
                        mod = (row.get(mod_col) or "").strip().upper()
                        anchor.add_modifiers([mod] if re.fullmatch(r"[A-Z0-9]{2}", mod) else [])
                        for c in other:
                            anchor.code_params.append({"code": mod, "relationship": "MODIFIER", "related_code": "",
                                                       "parameter_name": c, "parameter_value": row.get(c, ""),
                                                       "source": label})
                    if label not in anchor.attachments:
                        anchor.attachments.append(label)
                continue

            if kind == "ATTRIBUTE":
                code_cols = [c for c in t["columns"] if _is_code_col(c, t)]
                other = [c for c in t["columns"] if c not in code_cols]
                anchor = anchor_for(t, [])
                if anchor is None:
                    anchor = Rule(source_section=t["path"], source_type="table_only",
                                  text=" | ".join(t["texts"]), order=t["order"], source_page=t["page"], **common)
                    rules.append(anchor)
                for row in t["rows"]:
                    for cc in code_cols:
                        for code in extract_codes(row.get(cc, "")):
                            anchor.add_codes([code], label)
                            for oc in other:
                                if row.get(oc):
                                    anchor.code_params.append({"code": code, "relationship": "", "related_code": "",
                                                               "parameter_name": oc if len(code_cols) == 1 else f"{oc} ({cc})",
                                                               "parameter_value": row[oc], "source": label})
                if label not in anchor.attachments:
                    anchor.attachments.append(label)
                continue

            # TEXT table: rows that state a rule become rules (clubbed later if identical)
            for j, txt in enumerate(t["texts"]):
                if txt and has_directive(txt) and not boiler.is_boilerplate(txt):
                    r = Rule(source_section=f"{t['path']} > {t['header']}".strip(" >"), source_type="table",
                             text=txt, order=t["order"] + j / 1000.0, source_page=t["page"], **common)
                    r.add_codes(extract_codes(txt), "rule text")
                    rules.append(r)

        # ---- 3. club, policy-scope codes, ids -----------------------------------
        rules = club_rules(sorted(rules, key=lambda r: r.order))
        used_ids = set()
        for seq, r in enumerate(rules, 1):
            mod = r.extra.get("modifier_key", "")
            rid = f"{ID_PREFIX}-{slug}-M{mod}" if mod and re.fullmatch(r"[A-Z0-9]{2}", mod) else f"{ID_PREFIX}-{slug}-R{seq:02d}"
            while rid in used_ids:
                rid += "b"
            used_ids.add(rid)
            r.rule_id = rid
            finalize(r)
            if r.extra.get("modifier_row") and r.category not in ("MODIFIER_RULE", "PAYMENT_ADJUSTMENT"):
                r.secondary = [r.category] + [c for c in r.secondary if c != "MODIFIER_RULE"]
                r.category = "MODIFIER_RULE"
            fix_modifier_polarity(r)
            if r.code_params:
                r.attachment_values.append(summarize_params(r.code_params))
        all_rules.extend(rules)

    # the same rule printed in two policies (FQHC / RHC, ...): keep both, but point the later one at the first
    import difflib
    seen: List[Tuple[str, str, str]] = []
    for r in all_rules:
        if r.extra.get("modifier_row"):
            continue
        key = re.sub(r"\W+", " ", r.text.lower()).strip()
        if len(key) < 80:
            continue
        for rid, pk, k0 in seen:
            if pk != r.policy_key and abs(len(k0) - len(key)) < 0.2 * len(key) \
                    and difflib.SequenceMatcher(None, k0, key, autojunk=False).ratio() >= 0.9:
                r.extra["dup_of"] = rid
                break
        else:
            seen.append((r.rule_id, r.policy_key, key))

    for row in policy_code_list:
        row["Linked_Rule_ID"] = row.pop("_rule").rule_id if row.get("_rule") is not None else ""
        row.pop("_rule", None)

    records = [rule_record(r) for r in all_rules]
    for r, rec in zip(all_rules, records):
        add_trigger_guard(rec, r)
        # explicit ICD-10 lists (rule text or an attached table) are shown too, not only looked-up ones
        icd = r.extra.get("icd_codes") or (_sql_skeleton.parse_icd(r.text) if rec.get("SQL_Query") else [])
        if not rec.get("Possible_icd_codes") and icd:
            rec["Possible_icd_codes"] = _rule_core.compress_icd(icd)
    extra_summary = [
        ("Corpus", "Payer", PAYER),
        ("Corpus", "Scope", "Medicare Advantage"),
        ("Corpus", "Policies in input", len(policies)),
        ("Corpus", "Policies processed", len(ma_policies)),
        ("Corpus", "Non-Medicare policies excluded", len(excluded)),
        ("Corpus", "Worked-example tables skipped (placeholder codes)", skipped_example_tables),
        ("Corpus", "Boilerplate threshold (policies sharing a sentence)", boiler.threshold()),
    ]
    pcl_sheet = ("Policy_Code_List",
                 ["Policy_Title", "Table", "Code", "Code_Description", "Footnote_Marker", "Linked_Rule_ID"],
                 policy_code_list, {"Policy_Title": 40, "Table": 34, "Code_Description": 60, "Linked_Rule_ID": 18},
                 {"Code_Description"})
    written = export_xlsx_only(
        lambda d: export_all(all_rules, records, d, extra_summary, extra_sheets=[pcl_sheet], prefix="billing_rules_devoted"),
        out_dir, compare_dir)

    from collections import Counter
    print(f"Medicare policies processed: {len(ma_policies)}  (excluded non-Medicare: {len(excluded)})")
    print(f"Rules generated: {len(all_rules)}   rules with table code lists: {sum(1 for r in all_rules if r.attachments)}")
    print(f"Worked-example tables skipped: {skipped_example_tables}")
    for c, n in Counter(r.category for r in all_rules).most_common():
        print(f"  {c:28s} {n:5d}")
    for w in written:
        print(f"Written: {w}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Devoted deterministic billing rule generator")
    ap.add_argument("--input", default=str(INPUT_JSON))
    ap.add_argument("--out-dir", default=str(OUTPUT_DIR))
    ap.add_argument("--compare-dir", default=str(COMPARE_DIR), help="Rule Comparison folder ('' to disable)")
    ap.add_argument("--sql-dialect", default=None,
                    choices=["postgres", "sqlserver", "oracle", "snowflake", "sqlite"],
                    help="SQL dialect for the generated queries (default: SQL_DIALECT in sql_skeleton.py)")
    a = ap.parse_args()
    if a.sql_dialect:
        import sql_skeleton
        sql_skeleton.SQL_DIALECT = a.sql_dialect
    run_pipeline(Path(a.input), Path(a.out_dir), Path(a.compare_dir) if a.compare_dir else None)


if __name__ == "__main__":
    main()