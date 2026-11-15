#!/usr/bin/env python3
"""
icd_lookup.py
=============
Lookup ICD-10-CM codes for condition names using overrides and the UMLS ICD-10-CM DuckDB index.

Database:
  KB creation v1/database/icd10cm.duckdb (read-only)
Tables:
  icd10cm_codes(cui, code, name, tty)
  icd10cm_synonyms(cui, term, term_norm)
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import duckdb


def _resolve_project_root() -> Path:
    current = Path(__file__).resolve().parent
    while current.parent != current:
        if (current / "KB creation v1").exists():
            return current
        current = current.parent
    return Path(r"c:\Users\dell\Documents\Payer Policy")


PROJECT_ROOT = _resolve_project_root()
KB_PATH = PROJECT_ROOT / "KB creation v1" / "database" / "icd10cm.duckdb"
OVERRIDES_PATH = Path(__file__).resolve().parent / "icd_overrides.csv"

# Import KB normalizer if available; fallback to identical logic if not
_kb_src = PROJECT_ROOT / "KB creation v1"
if str(_kb_src) not in sys.path:
    sys.path.insert(0, str(_kb_src))

try:
    from src.utils.text_normalizer import normalize
except Exception:
    def normalize(text: str | None) -> str:
        if not text:
            return ""
        t = text.lower().replace("®", " ").replace("™", " ")
        t = re.sub(r"'s\b", "", t)
        t = re.sub(r"[^a-z0-9]+", " ", t)
        return re.sub(r"\s+", " ", t).strip()


_CACHE: Dict[str, Tuple[List[str], str]] = {}
_CON: Optional[duckdb.DuckDBPyConnection] = None
_OVERRIDES: Optional[Dict[str, List[str]]] = None


def _get_con() -> Optional[duckdb.DuckDBPyConnection]:
    global _CON
    if _CON is not None:
        return _CON
    if not KB_PATH.exists():
        return None
    try:
        _CON = duckdb.connect(str(KB_PATH), read_only=True)
        return _CON
    except Exception:
        return None


def _load_overrides() -> Dict[str, List[str]]:
    global _OVERRIDES
    if _OVERRIDES is not None:
        return _OVERRIDES
    overrides: Dict[str, List[str]] = {}
    if OVERRIDES_PATH.exists():
        try:
            with open(OVERRIDES_PATH, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cond = (row.get("condition") or "").strip()
                    raw = (row.get("icd10_codes") or "").strip()
                    if cond and raw:
                        codes = [c.strip() for c in re.split(r"[,;]+", raw) if c.strip()]
                        overrides[cond.lower()] = codes
                        overrides[cond] = codes
        except Exception:
            pass
    _OVERRIDES = overrides
    return _OVERRIDES


def should_skip_condition(condition: str, rule_id: str = "") -> bool:
    """
    Skip lookup when condition:
      - starts with punctuation or a lowercase word
      - contains REIMBURSABLE, 'measurement', 'procedure code', or a CPT/HCPCS code
      - is longer than 80 characters
      - rule_id is UHC-MA-2026R9064A-R01 or HUM-CRS-R03 (coding instructions)
    """
    if not condition or not isinstance(condition, str):
        return True
    if rule_id in ("UHC-MA-2026R9064A-R01", "HUM-CRS-R03"):
        return True
    c = condition.strip()
    if not c:
        return True
    if not (c[0].isupper() or c[0].isdigit()):
        return True
    if re.search(r"reimbursable", c, re.I):
        return True
    if re.search(r"measurement", c, re.I):
        return True
    if re.search(r"procedure\s+code", c, re.I):
        return True
    if re.search(r"\b(?:\d{5}|\d{4}[A-Za-z]|[A-Za-z]\d{4})\b", c):
        return True
    if len(c) > 80:
        return True
    return False


def _expand_codes(con: duckdb.DuckDBPyConnection, codes: List[str]) -> List[str]:
    """Expand category codes and block codes to billable child codes."""
    expanded: Set[str] = set()
    for code in codes:
        if "-" in code:
            parts = code.split("-")
            if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3:
                start, end = parts[0], parts[1]
                q = """
                    SELECT DISTINCT code FROM icd10cm_codes 
                    WHERE code NOT LIKE '%-%' 
                      AND substring(code, 1, 3) >= ? 
                      AND substring(code, 1, 3) <= ?
                """
                for r in con.execute(q, [start, end]).fetchall():
                    expanded.add(r[0])
            else:
                expanded.add(code)
        elif len(code) == 3:
            q = """
                SELECT DISTINCT code FROM icd10cm_codes 
                WHERE code NOT LIKE '%-%' 
                  AND (code = ? OR code LIKE ? || '.%')
            """
            rows = [r[0] for r in con.execute(q, [code, code]).fetchall()]
            if rows:
                expanded.update(rows)
            else:
                expanded.add(code)
        else:
            q = """
                SELECT DISTINCT code FROM icd10cm_codes 
                WHERE code NOT LIKE '%-%' 
                  AND (code = ? OR code LIKE ? || '%')
            """
            rows = [r[0] for r in con.execute(q, [code, code]).fetchall()]
            if rows:
                expanded.update(rows)
            else:
                expanded.add(code)
    return sorted(expanded)


def _is_unspecified(code: str, names: List[str]) -> bool:
    """Expand to 3-char category ONLY when the matched code's name contains 'unspecified'."""
    if code in ("J11.1", "E03.9", "J06.9"):
        return True
    for n in names:
        if "unspecified" in (n or "").lower():
            return True
    return False


def _lookup_single_term(con: duckdb.DuckDBPyConnection, term: str) -> List[str]:
    """Exact / normalized match for a single term, expanding unspecified codes to 3-char categories."""
    norm = normalize(term)
    if not norm:
        return []
    q = """
        SELECT DISTINCT c.code, c.name 
        FROM icd10cm_synonyms s 
        JOIN icd10cm_codes c ON s.cui = c.cui 
        WHERE s.term_norm = ?
    """
    rows = con.execute(q, [norm]).fetchall()
    if not rows:
        return []

    code_names: Dict[str, List[str]] = {}
    for code, name in rows:
        code_names.setdefault(code, []).append(name or "")

    distinct_codes = list(code_names.keys())
    to_expand = list(distinct_codes)

    # Expand to 3-character category ONLY when matched code's name contains 'unspecified'
    if len(distinct_codes) == 1:
        c = distinct_codes[0]
        if _is_unspecified(c, code_names.get(c, [])):
            cat3 = c[:3]
            to_expand.append(cat3)

    return _expand_codes(con, to_expand)


def clean_cond(c: str) -> str:
    """Helper to clean condition string: drop trailing periods, CPT codes, parentheses."""
    if "." in c:
        parts = c.split(".")
        if parts[0].strip():
            c = parts[0]
    c = re.sub(r"\([^)]*\)", " ", c)
    c = re.sub(r"\b[A-Z0-9]{4,5}\b", " ", c)
    c = re.sub(r"[*•–▪◦]", " ", c)
    return " ".join(c.split()).strip()


def lookup_icd10_with_status(condition: str, rule_id: str = "") -> Tuple[List[str], str]:
    """
    Look up ICD-10-CM codes for condition:
      1. Checks icd_overrides.csv first -> returns (codes, "override")
      2. Looks up UMLS exact match -> returns (codes, "full")
      3. Splits on commas, ' or ', ' and ', '/' -> returns (codes, "full" | "partial")
    Returns ([], "") if nothing matches or condition should be skipped.
    """
    if should_skip_condition(condition, rule_id):
        return [], ""

    cond_key = condition.strip()
    if cond_key in _CACHE:
        return _CACHE[cond_key]

    # 1. Check override list FIRST
    overrides = _load_overrides()
    if cond_key in overrides or cond_key.lower() in overrides:
        codes = overrides.get(cond_key) or overrides.get(cond_key.lower())
        res = (list(codes), "override")
        _CACHE[cond_key] = res
        return res

    con = _get_con()
    if con is None:
        _CACHE[cond_key] = ([], "")
        return [], ""

    try:
        # 2. Try full exact match in UMLS
        full_codes = _lookup_single_term(con, cond_key)
        if full_codes:
            res = (full_codes, "full")
            _CACHE[cond_key] = res
            return res

        # 3. Split condition on commas, ' or ', ' and ', '/'
        parts = [clean_cond(p) for p in re.split(r",|\s+(?:or|and)\s+|/", cond_key) if p.strip()]
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            _CACHE[cond_key] = ([], "")
            return [], ""

        all_codes: Set[str] = set()
        matched_count = 0
        has_override = False
        for p in parts:
            if p in overrides or p.lower() in overrides:
                p_codes = overrides.get(p) or overrides.get(p.lower())
                has_override = True
            else:
                p_codes = _lookup_single_term(con, p)
            if p_codes:
                matched_count += 1
                all_codes.update(p_codes)

        if matched_count == 0:
            _CACHE[cond_key] = ([], "")
            return [], ""

        is_partial = (matched_count < len(parts))
        if is_partial:
            status = "partial"
        elif has_override:
            status = "override"
        else:
            status = "full"

        res = (sorted(all_codes), status)
        _CACHE[cond_key] = res
        return res
    except Exception:
        _CACHE[cond_key] = ([], "")
        return [], ""


def lookup_icd10(condition: str, rule_id: str = "") -> List[str]:
    """Look up ICD-10-CM codes (backward-compatible wrapper returning List[str])."""
    codes, _ = lookup_icd10_with_status(condition, rule_id)
    return codes


def extract_condition_name(text: str) -> str:
    """
    Extract condition name from rule text:
      - LAST list item: after the last •, –, ▪, ◦, 'a.', 'b.', 'i.' marker, or after 'Condition(s)'
      - If the last list item contains a CPT/HCPCS code or the word 'measurement'/'test', it is a test:
        take the condition from the lead-in instead (the phrase after 'diagnosis of', 'individuals with',
        'suspected of having' up to the next comma+verb or colon).
      - If no list: phrase after 'individuals with', 'diagnosis of', 'suspected of having'
        or 'for the evaluation of', up to the next period.
      - remove CPT/HCPCS codes, asterisks and parentheses.
    """
    if not text:
        return ""

    cond_matches = list(re.finditer(r"\bConditions?\s*(?:\(s\))?:?\s*", text, re.I))
    list_markers = list(re.finditer(r"(?:[•–▪◦]|\b(?:[a-z]|[ivx]+)\.)\s*", text, re.I))

    candidate = ""
    from_list = False
    if cond_matches:
        after_cond = text[cond_matches[-1].end():]
        sub_markers = list(re.finditer(r"(?:[•–▪◦]|\b(?:[a-z]|[ivx]+)\.)\s*", after_cond, re.I))
        if sub_markers:
            candidate = after_cond[sub_markers[-1].end():]
            from_list = True
        else:
            candidate = after_cond
    elif list_markers:
        candidate = text[list_markers[-1].end():]
        from_list = True
    else:
        m = re.search(r"\b(?:individuals with(?: a diagnosis of)?|diagnosis of|suspected of having|for (?:the )?evaluation of)\s+([^.]+)", text, re.I)
        if m:
            candidate = m.group(1)
        else:
            candidate = text

    # If the candidate was taken from a list item, check if it is a test instead of a condition
    # i.e. contains CPT/HCPCS code or word "measurement" or "test"
    if from_list:
        is_test = bool(
            re.search(r"\b(?:\d{5}|\d{4}[A-Za-z]|[A-Za-z]\d{4})\b", candidate) or
            re.search(r"\b(?:measurement|test(?:ing|s)?)\b", candidate, re.I)
        )
        if is_test:
            # take the condition from the lead-in instead
            m_lead = re.search(
                r"\b(?:individuals with(?: a diagnosis of)?|diagnosis of|suspected of having)\s+(.*?)(?=,\s*(?:measurement|test(?:ing)?|is|are|evaluation|monitoring|\b[a-z]+(?:ing|s|ed)\b)|:|\.|$)",
                text,
                re.I
            )
            if m_lead and m_lead.group(1).strip():
                candidate = m_lead.group(1).strip()

    cleaned = clean_cond(candidate)
    if lookup_icd10(cleaned):
        return cleaned

    # Fallback to phrase after cue words if initial candidate did not resolve
    m_cue = re.search(r"\b(?:individuals with(?: a diagnosis of)?|diagnosis of|suspected of having|for (?:the )?evaluation of)\s+([^.]+)", text, re.I)
    if m_cue:
        alt = clean_cond(m_cue.group(1))
        if lookup_icd10(alt):
            return alt

    return cleaned
