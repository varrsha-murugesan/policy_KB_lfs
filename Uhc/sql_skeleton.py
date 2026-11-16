#!/usr/bin/env python3
"""
sql_skeleton.py
===============
Single-table SQL skeleton + one query per billing rule.

All queries read ONE table (one row per claim line, carrying claim, member, provider,
diagnosis, modifier and NDC fields). History checks ("billed before within 30 days",
"primary code on the same date") join the table to itself. Per-code values from the
policy attachments (time spans, age ranges, add-on -> primary pairs, code families,
NDC unit limits) are written into the query as inline VALUES lists, so no second
table is needed.

Each query returns the claim lines that BREAK the rule (audit candidates).

-------------------------------------------------------------------------------
CONFIGURE HERE  ->  table name, your column names, SQL dialect, status values
-------------------------------------------------------------------------------
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ==============================================================================
# 1. YOUR TABLE  (edit this block only)
# ==============================================================================

CLAIM_TABLE = "claim_lines"          # the single table holding every claim line

# postgres | sqlserver | oracle | snowflake | sqlite
SQL_DIALECT = "postgres"

# logical name used by the generator  ->  physical column name in YOUR table
COLUMNS: Dict[str, str] = {
    "claim_id":          "claim_id",
    "line_num":          "line_num",
    "member_id":         "member_id",
    "member_dob":        "member_dob",
    "member_gender":     "member_gender",
    "line_of_business":  "line_of_business",
    "claim_type":        "claim_type",          # professional / institutional
    "billing_tin":       "billing_provider_tin",
    "rendering_npi":     "rendering_provider_npi",
    "bill_type":         "bill_type",
    "place_of_service":  "place_of_service",
    "proc_code":         "proc_code",           # CPT / HCPCS
    "modifier_1":        "modifier_1",
    "modifier_2":        "modifier_2",
    "modifier_3":        "modifier_3",
    "modifier_4":        "modifier_4",
    "rev_code":          "revenue_code",
    "diag_code_1":       "diag_code_1",
    "diag_code_2":       "diag_code_2",
    "diag_code_3":       "diag_code_3",
    "diag_code_4":       "diag_code_4",
    "diag_code_5":       "diag_code_5",
    "diag_code_6":       "diag_code_6",
    "diag_code_7":       "diag_code_7",
    "diag_code_8":       "diag_code_8",
    "diag_code_9":       "diag_code_9",
    "diag_code_10":      "diag_code_10",
    "diag_code_11":      "diag_code_11",
    "diag_code_12":      "diag_code_12",
    "serv_from_date":    "service_from_date",
    "serv_to_date":      "service_to_date",
    "units_billed":      "units_billed",
    "allowed_amt":       "allowed_amount",
    "paid_amt":          "paid_amount",
    "line_status":       "line_status",
    "auth_number":       "auth_number",
    "ndc_code":          "ndc_code",
    "ndc_units":         "ndc_units",
}
MODIFIER_COLS = ["modifier_1", "modifier_2", "modifier_3", "modifier_4"]
DIAG_COLS = [f"diag_code_{i}" for i in range(1, 13)]

# values stored in your table
LOB_VALUES = {"Medicare Advantage": "MEDICARE_ADVANTAGE", "Commercial": "COMMERCIAL"}
CLAIM_TYPE_VALUES = {"Professional": "PROFESSIONAL", "Institutional": "INSTITUTIONAL"}
DENIED_STATUS = "DENIED"             # lines with this status are ignored (not paid)
GENDER_VALUES = {"male": "M", "female": "F"}
ICD_STORED_WITH_DOT = False          # E11.9 stored as 'E119' -> False

# column types for the CREATE TABLE skeleton
_TYPES = {
    "member_dob": "DATE", "serv_from_date": "DATE", "serv_to_date": "DATE",
    "units_billed": "NUMERIC(10,2)", "ndc_units": "NUMERIC(12,3)",
    "allowed_amt": "NUMERIC(12,2)", "paid_amt": "NUMERIC(12,2)", "line_num": "INTEGER",
}


# ==============================================================================
# 2. DIALECT HELPERS
# ==============================================================================

def col(alias: str, logical: str, used: Optional[Set[str]] = None) -> str:
    if used is not None:
        used.add(logical)
    return f"{alias}.{COLUMNS[logical]}"


def date_minus(expr: str, n: int, unit: str) -> str:
    if unit == "week":
        n, unit = n * 7, "day"
    d = SQL_DIALECT
    if d in ("postgres",):
        return f"({expr} - INTERVAL '{n} {unit}')"
    if d in ("sqlserver", "snowflake"):
        return f"DATEADD({unit}, -{n}, {expr})"
    if d == "oracle":
        return {"day": f"({expr} - {n})", "month": f"ADD_MONTHS({expr}, -{n})",
                "year": f"ADD_MONTHS({expr}, -{12 * n})"}[unit]
    if d == "sqlite":
        return f"date({expr}, '-{n} {unit}s')"
    raise ValueError(SQL_DIALECT)


def same_period(a: str, b: str, period: str) -> str:
    d = SQL_DIALECT
    if d == "sqlite":
        fmt = "%Y-%m" if period == "month" else "%Y"
        return f"strftime('{fmt}', {a}) = strftime('{fmt}', {b})"
    if d == "sqlserver":
        y = f"YEAR({a}) = YEAR({b})"
        return y if period == "year" else f"{y} AND MONTH({a}) = MONTH({b})"
    y = f"EXTRACT(YEAR FROM {a}) = EXTRACT(YEAR FROM {b})"
    return y if period == "year" else f"{y} AND EXTRACT(MONTH FROM {a}) = EXTRACT(MONTH FROM {b})"


def age_in_days(dob: str, dos: str) -> str:
    d = SQL_DIALECT
    if d in ("postgres", "oracle"):
        return f"({dos} - {dob})"
    if d in ("sqlserver", "snowflake"):
        return f"DATEDIFF(day, {dob}, {dos})"
    if d == "sqlite":
        return f"(julianday({dos}) - julianday({dob}))"
    raise ValueError(SQL_DIALECT)


def values_cte(name: str, cols: List[str], rows: List[Tuple[Any, ...]]) -> str:
    def lit(v):
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    if SQL_DIALECT == "oracle":
        body = "\n    UNION ALL ".join(
            "SELECT " + ", ".join(f"{lit(v)} AS {c}" for v, c in zip(r, cols)) + " FROM dual" for r in rows)
        return f"{name} AS (\n    {body}\n)"
    vals = ",\n        ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows)
    if SQL_DIALECT == "sqlite":
        return f"{name}({', '.join(cols)}) AS (VALUES\n        {vals}\n)"
    return f"{name}({', '.join(cols)}) AS (\n    SELECT * FROM (VALUES\n        {vals}\n    ) AS v({', '.join(cols)})\n)"


def in_list(values: List[str]) -> str:
    return "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"


# ==============================================================================
# 3. TABLE SKELETON (DDL)
# ==============================================================================

def table_ddl() -> str:
    lines = []
    for logical, physical in COLUMNS.items():
        t = _TYPES.get(logical, "VARCHAR(50)")
        lines.append(f"    {physical:<26} {t}")
    return (f"-- One row per claim line. Every rule query reads only this table.\n"
            f"CREATE TABLE {CLAIM_TABLE} (\n" + ",\n".join(lines) +
            f",\n    PRIMARY KEY ({COLUMNS['claim_id']}, {COLUMNS['line_num']})\n);")


# ==============================================================================
# 4. VALUES READ FROM THE RULE TEXT
# ==============================================================================

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "thirty": 30, "sixty": 60, "ninety": 90}
_N = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirty|sixty|ninety)"
_U = r"(day|week|month|year)s?"


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM[tok.lower()]


def parse_frequency(text: str) -> Optional[Dict[str, Any]]:
    """Frequency limit stated in the rule text -> {'kind': ..., 'n': ..., 'unit': ...}."""
    t = text.lower()
    if re.search(r"once (?:per|in a|in the|during (?:a|the)(?: patient'?s?)?) lifetime|\bper lifetime\b", t):
        return {"kind": "lifetime"}
    m = (re.search(rf"(?:once|one(?: \w+){{0,2}}|no more than (?:once|one(?: \w+){{0,2}})) every {_N} {_U}", t)
         or re.search(rf"no sooner than {_N} {_U}", t) or re.search(rf"\bevery {_N} {_U}", t))
    if m:
        n, unit = _n(m.group(1)), m.group(2)
        if unit == "week":
            n, unit = n * 7, "day"
        return {"kind": "interval", "n": n, "unit": unit}
    m = re.search(r"(?:once|one(?: \w+){0,2}) (?:per|a|each|every) (calendar )?(day|week|month|year)\b|\bannually\b|"
                  r"once per (?:that )?(?:individual )?(month)", t)
    if m:
        unit = next((g for g in m.groups()[1:] if g), "year")
        if unit == "day":
            return {"kind": "per_day"}
        if unit == "week":
            return {"kind": "interval", "n": 7, "unit": "day"}
        return {"kind": "calendar", "unit": unit}
    if re.search(r"(?:once|one(?: \w+){0,2}) per date of service|\bper date of service\b|\bper day\b", t):
        return {"kind": "per_day"}
    m = re.search(rf"(?:maximum|max|no more than|not to exceed|limited to|up to)(?: of)? {_N} units?", t)
    if m:
        return {"kind": "max_units", "n": _n(m.group(1))}
    return None


_AGE_NUM = (r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
            r"sixteen|seventeen|eighteen|nineteen|twenty)")
_AGE_WORDS = dict(_NUM, thirteen=13, fourteen=14, fifteen=15, sixteen=16, seventeen=17, eighteen=18, nineteen=19,
                  twenty=20)
_AGE_UNIT = r"(?:\s*(years?|yrs?|months?|weeks?|days?))?"


def _age_n(tok: str) -> int:
    return int(tok) if tok.isdigit() else _AGE_WORDS[tok.lower()]


def _age_unit(tok: Optional[str]) -> str:
    t = (tok or "year").lower()
    return "year" if t.startswith("y") else "month" if t.startswith("mo") else "week" if t.startswith("w") else "day"


# (regex, kind) - kind: range | min_incl (age >= n) | min_excl (age > n) | max_excl (age < n) | max_incl (age <= n)
_AGE_PATTERNS = [
    (rf"\b(?:ages?|aged|between the ages of|from)\s+{_AGE_NUM}{_AGE_UNIT}\s*(?:-|–|to|through|and)\s*{_AGE_NUM}{_AGE_UNIT}", "range"),
    (rf"\b{_AGE_NUM}{_AGE_UNIT}\s*(?:-|–|to|through)\s*{_AGE_NUM}{_AGE_UNIT}\s+(?:of age|old)\b", "range"),
    (rf"\b(?:age[sd]?\s+)?{_AGE_NUM}{_AGE_UNIT}\s*(?:of age\s*)?(?:or|and)\s+(?:older|over|above|greater)\b", "min_incl"),
    (rf"\b(?:at least|greater than or equal to|>=|≥)\s+{_AGE_NUM}{_AGE_UNIT}\s*(?:of age|old)", "min_incl"),
    (rf"\b(?:older than|over the age of|over|above the age of|greater than|more than|>)\s+{_AGE_NUM}{_AGE_UNIT}\s*(?:of age|old)?", "min_excl"),
    (rf"\b(?:age|aged)\s+(?:is\s+)?(?:greater|more|older) than\s+{_AGE_NUM}{_AGE_UNIT}", "min_excl"),
    (rf"\b(?:younger than|under the age of|under|less than|below the age of|below|<)\s+{_AGE_NUM}{_AGE_UNIT}\s*(?:of age|old)?", "max_excl"),
    (rf"\b(?:age|aged)\s+(?:is\s+)?(?:less|younger) than\s+{_AGE_NUM}{_AGE_UNIT}", "max_excl"),
    (rf"\b{_AGE_NUM}{_AGE_UNIT}\s*(?:of age\s*)?(?:or|and)\s+(?:younger|under|less|below)\b", "max_incl"),
    (rf"\b(?:up to|through|less than or equal to|<=|≤)\s+(?:age\s+)?{_AGE_NUM}{_AGE_UNIT}\s*(?:of age|old)", "max_incl"),
]
_AGE_CONTEXT = re.compile(r"\bage[sd]?\b|\bof age\b|\byears? old\b|\bchildren\b|\badults?\b|\bindividuals?\b|"
                          r"\bpatients?\b|\bmembers?\b|\bpersons?\b|\binfants?\b|\bnewborns?\b", re.I)


def parse_age_bounds(text: str) -> List[Tuple[str, int, str]]:
    """Age restriction stated in the text -> [(kind, n, unit)], kind in min_incl/min_excl/max_excl/max_incl.
    Returns [] when no age restriction is stated, or when the text lists several age brackets
    (e.g. codes grouped 'under 2, 2-11, 11-19, 20 and older') - that is not one restriction."""
    found: List[Tuple[int, str, int, str]] = []
    taken: List[Tuple[int, int]] = []
    for rx, kind in _AGE_PATTERNS:
        for m in re.finditer(rx, text, re.I):
            if any(s <= m.start() < e or s < m.end() <= e for s, e in taken):
                continue
            window = text[max(0, m.start() - 40): m.end() + 25]
            unit_given = [g for g in m.groups() if g and not re.fullmatch(_AGE_NUM, g, re.I)]
            # needs age context, and a time unit of days/weeks/months only with explicit 'of age' / 'old'
            if not _AGE_CONTEXT.search(window):
                continue
            if unit_given and _age_unit(unit_given[0]) != "year" and not re.search(r"of age|old\b|age", window, re.I):
                continue
            nums = [g for g in m.groups() if g and re.fullmatch(_AGE_NUM, g, re.I)]
            units = [_age_unit(g) for g in m.groups() if g and not re.fullmatch(_AGE_NUM, g, re.I)]
            unit = units[-1] if units else "year"
            if kind == "range":
                found.append((m.start(), "min_incl", _age_n(nums[0]), units[0] if len(units) == 2 else unit))
                found.append((m.start(), "max_incl", _age_n(nums[1]), unit))
            else:
                found.append((m.start(), kind, _age_n(nums[0]), unit))
            taken.append((m.start(), m.end()))
    bounds = [(k, n, u) for _, k, n, u in sorted(found)]
    mins = {(k, n, u) for k, n, u in bounds if k.startswith("min")}
    maxs = {(k, n, u) for k, n, u in bounds if k.startswith("max")}
    if len(mins) > 1 or len(maxs) > 1:
        return []          # several brackets: not a single restriction
    return sorted(mins | maxs)


_MALE_RE = re.compile(r"\b(?:males?|men|boys?)\b", re.I)
_FEMALE_RE = re.compile(r"\b(?:females?|women|girls?|pregnant)\b", re.I)
_GENDER_IDENTITY_RE = re.compile(r"gender[- ]?(?:dysphori|incongruen|affirm|identit|transition|reassign|non-?conform|"
                                 r"diverse|expression)|transgender|intersex", re.I)


def parse_gender(text: str) -> Optional[str]:
    """'M' / 'F' when the rule is limited to one sex; None otherwise (both, neither, or gender-identity wording)."""
    if _GENDER_IDENTITY_RE.search(text):
        return None
    male, female = bool(_MALE_RE.search(text)), bool(_FEMALE_RE.search(text))
    if male == female:
        return None
    return GENDER_VALUES["male"] if male else GENDER_VALUES["female"]


def age_gender_population(text: str, alias: str, used: Set[str]) -> Tuple[Optional[str], str]:
    """SQL for the population the rule is written for, using member_dob vs the service date:
       '18 years of age or older' -> member_dob <= service_from_date - 18 years.
    Returns (condition, readable description) or (None, '')."""
    dob, dos = col(alias, "member_dob", set()), col(alias, "serv_from_date", set())
    conds, desc = [], []
    for kind, n, unit in parse_age_bounds(text):
        if kind == "min_incl":
            conds.append(f"{dob} <= {date_minus(dos, n, unit)}"); desc.append(f"age >= {n} {unit}s")
        elif kind == "min_excl":
            conds.append(f"{dob} < {date_minus(dos, n, unit)}"); desc.append(f"age > {n} {unit}s")
        elif kind == "max_excl":
            conds.append(f"{dob} > {date_minus(dos, n, unit)}"); desc.append(f"age < {n} {unit}s")
        elif kind == "max_incl":
            conds.append(f"{dob} > {date_minus(dos, n + 1, unit)}"); desc.append(f"age <= {n} {unit}s")
    if conds:
        used.update({"member_dob", "serv_from_date"})
    g = parse_gender(text)
    if g:
        conds.append(f"{col(alias, 'member_gender', used)} = '{g}'"); desc.append(f"gender = {g}")
    if not conds:
        return None, ""
    return " AND ".join(conds), ", ".join(desc)


def parse_age_param(v: str) -> Optional[Tuple[int, int]]:
    """Attachment value '6 months – 5 years' -> (min_days, max_days), inclusive."""
    m = re.match(r"\s*(\d+)\s*(day|week|month|year)s?\s*–\s*(\d+)\s*(day|week|month|year)s?", v)
    if not m:
        return None
    f = {"day": 1, "week": 7, "month": 30.44, "year": 365.25}
    lo = int(int(m.group(1)) * f[m.group(2)])
    hi = int((int(m.group(3)) + 1) * f[m.group(4)]) - 1
    return lo, hi


def parse_time_span(v: str) -> Optional[Tuple[str, int]]:
    v = v.strip().lower()
    m = re.match(r"(\d+)\s*days?", v)
    if m:
        return "DAYS", int(m.group(1))
    if "calendar month" in v:
        return "CALENDAR_MONTH", 0
    if "calendar year" in v:
        return "CALENDAR_YEAR", 0
    return None


ICD_RE = re.compile(r"\b([A-TV-Z]\d{2}\.[0-9A-Z]{1,4}|[A-TV-Z]\d{2}(?=\s*(?:-|–|,|\)|;|\s+(?:or|and)\b)))")
# codes mentioned as an example, a coding-sequence instruction or a category range are not a
# covered / excluded diagnosis list ("For example, code Z51.89", "(category T86) first, followed by",
# "codes (V00-Y99)"); 'ANSI X12' is the claim transaction standard, not a diagnosis
_ICD_SKIP_BEFORE = re.compile(r"(?:for example|e\.g\.?|such as|category|categories|assign|ansi|standards?|followed by|first)[^.;]{0,25}$", re.I)
_ICD_SKIP_AFTER = re.compile(r"^\)?\s*(?:first|followed by|837)", re.I)
_ICD_RANGE = re.compile(r"\b[A-TV-Z]\d{2}(?:\.[0-9A-Z]{1,4})?\s*[-–]\s*[A-TV-Z]\d{2}(?:\.[0-9A-Z]{1,4})?\b")


# ICD-10 lookup hook ---------------------------------------------------------------
# Set ICD_LOOKUP to a function  f(rule) -> List[Tuple[str, List[str]]]  returning
# [(condition_name, [icd10 codes]), ...] for the conditions named in the rule text.
# It is called ONLY for rules whose query would otherwise need
# "ICD-10 codes for the conditions named in the rule".
ICD_LOOKUP = None
# When ICD_LOOKUP is not set, use icd_lookup.py (UMLS ICD-10-CM index, KB creation v1/database/icd10cm.duckdb)
# if it can be found: next to this file, in a parent folder, or in the folder named by the ICD_LOOKUP_DIR
# environment variable. Set to False to switch the lookup off.
ICD_LOOKUP_AUTO = True
_ICD_AUTO_TRIED = False


def _auto_icd_lookup():
    """Wrap icd_lookup.py as an ICD_LOOKUP function. Returns None (with one message) when it is not available."""
    import importlib
    import os
    here = Path(__file__).resolve().parent
    dirs = [Path(os.environ["ICD_LOOKUP_DIR"])] if os.environ.get("ICD_LOOKUP_DIR") else []
    for d in [here] + list(here.parents)[:3]:
        dirs += [d, d / "KB creation v1", d / "KB creation v1" / "scripts", d / "KB creation v1" / "src"]
    hit = next((d for d in dirs if (d / "icd_lookup.py").exists()), None)
    if hit is None:
        print("[ICD] icd_lookup.py not found - rules naming conditions keep the ICD-10 TODO")
        return None
    if str(hit) not in sys.path:
        sys.path.insert(0, str(hit))
    try:
        mod = importlib.import_module("icd_lookup")
    except Exception as e:
        print(f"[ICD] icd_lookup.py could not be imported ({e}) - ICD-10 lookup off")
        return None
    if not Path(mod.KB_PATH).exists():
        print(f"[ICD] {mod.KB_PATH} not found - run build_icd10cm_index.py first; ICD-10 lookup off")
        return None
    print(f"[ICD] ICD-10 lookup on: {hit / 'icd_lookup.py'} -> {mod.KB_PATH}")

    def lookup(rule) -> List[Tuple[str, List[str]]]:
        cond = mod.extract_condition_name(getattr(rule, "text", "") or "")
        codes, status = mod.lookup_icd10_with_status(cond, getattr(rule, "rule_id", "") or "")
        if not codes:
            return []
        return [(cond if status in ("full", "override") else f"{cond} [{status} match]", codes)]
    return lookup


def _lookup_icd(rule) -> List[Tuple[str, List[str]]]:
    global ICD_LOOKUP, _ICD_AUTO_TRIED
    if ICD_LOOKUP is None and ICD_LOOKUP_AUTO and not _ICD_AUTO_TRIED:
        _ICD_AUTO_TRIED = True
        ICD_LOOKUP = _auto_icd_lookup()
    if ICD_LOOKUP is None:
        return []
    try:
        out = ICD_LOOKUP(rule) or []
    except Exception:
        return []
    clean = []
    for cond, codes in out:
        codes = [(x if ICD_STORED_WITH_DOT else x.replace(".", "")).upper() for x in codes if x]
        if codes:
            clean.append((cond, list(dict.fromkeys(codes))))
    return clean


def parse_icd(text: str) -> List[str]:
    """ICD-10-CM codes the rule lists as covered / excluded diagnoses (not examples or ranges)."""
    if not re.search(r"icd|diagnos", text, re.I):
        return []
    ranges = [m.span() for m in _ICD_RANGE.finditer(text)]
    out = []
    for m in ICD_RE.finditer(text):
        if any(s <= m.start() < e for s, e in ranges):
            continue
        if _ICD_SKIP_BEFORE.search(text[max(0, m.start() - 60):m.start()]) or _ICD_SKIP_AFTER.search(text[m.end():m.end() + 25]):
            continue
        c = m.group(1).upper()
        out.append(c if ICD_STORED_WITH_DOT else c.replace(".", ""))
    return list(dict.fromkeys(out))


POS_RE = re.compile(r"(?:\bPOS\b|place of service)(?: code)?s?\s*\(?(\d{2})\b", re.I)


def parse_pos(text: str) -> List[str]:
    return list(dict.fromkeys(POS_RE.findall(text)))


def parse_percent(text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", text)))


_NEGATIVE = re.compile(r"\bnot (?:be )?(?:reimburs|cover|payable|allowed|eligible)|\bnon-?(?:reimbursable|covered)|"
                       r"\bwill not\b|\bdo not\b|\bdoes not\b|\bdeny|\bdenied\b|\bexclud", re.I)


REQUIRED_DX_POSITIVE = False    # True: "must include one of these diagnoses ... denied if not present" is a REQUIREMENT list
_DX_REQUIRED_RE = re.compile(r"(?:must|required?|need|only)\b[^.]{0,100}\b(?:diagnos[a-z]*|icd-?10)|"
                             r"(?:diagnos[a-z]*|icd-?10)[^.]{0,80}\b(?:are|is) required\b|"
                             r"\bone of the (?:following |listed )?(?:diagnos[a-z]*|icd-?10)", re.I)
_DX_EXCLUDING_RE = re.compile(r"\bexcept\b|\bexclud|\bnot (?:be )?(?:cover|reimburs|payable|eligible)|\bnon-?covered", re.I)


def is_negative(text: str) -> bool:
    return bool(_NEGATIVE.search(text))


# ==============================================================================
# 5. ONE QUERY PER RULE
# ==============================================================================

def _earlier(h: str, c: str, used: Set[str]) -> str:
    """h happened before c (ties broken by claim / line so only the 2nd+ occurrence is flagged)."""
    hd, cd = col(h, "serv_from_date", used), col(c, "serv_from_date", used)
    hc, cc = col(h, "claim_id", used), col(c, "claim_id", used)
    hl, cl = col(h, "line_num", used), col(c, "line_num", used)
    return f"({hd} < {cd} OR ({hd} = {cd} AND ({hc} < {cc} OR ({hc} = {cc} AND {hl} < {cl}))))"


def _active(alias: str, used: Set[str]) -> str:
    return f"{col(alias, 'line_status', used)} <> '{DENIED_STATUS}'"


def _slot(alias: str, logical: str, used: Set[str]) -> str:
    """An optional claim slot (modifier_2, diag_code_5 ...). Empty slots are NULL, and 'NOT (NULL IN (...) OR ...)'
    is NULL, not TRUE - so a 'required modifier / diagnosis missing' check would never flag a line that has an empty
    slot (nearly every line). COALESCE makes an empty slot a value that matches nothing."""
    return f"COALESCE({col(alias, logical, used)}, '~')"


def _any_mod(alias: str, mods: List[str], used: Set[str]) -> str:
    return "(" + " OR ".join(f"{_slot(alias, m, used)} IN {in_list(mods)}" for m in MODIFIER_COLS) + ")"


def _any_dx(alias: str, codes: List[str], used: Set[str]) -> str:
    """Any of the 12 diagnosis slots holds one of the codes. The code list is written ONCE (not once per slot), which
    keeps long ICD-10 lists readable and under Excel's cell limit. EXISTS is never NULL, so 'NOT' works on empty slots."""
    if SQL_DIALECT == "oracle":       # no VALUES table constructor before Oracle 23c
        return "(" + " OR ".join(f"{_slot(alias, d, used)} IN {in_list(codes)}" for d in DIAG_COLS) + ")"
    slots = ", ".join(f"({col(alias, d, used)})" for d in DIAG_COLS)
    if SQL_DIALECT == "sqlite":
        return f"EXISTS (SELECT 1 FROM (VALUES {slots}) WHERE column1 IN {in_list(codes)})"
    return f"EXISTS (SELECT 1 FROM (VALUES {slots}) AS dx(code) WHERE dx.code IN {in_list(codes)})"


def _params(rule, name: str) -> List[Dict[str, str]]:
    return [p for p in rule.code_params if p.get("parameter_name") == name and p.get("code")]


def _rel(rule, rel: str) -> List[Dict[str, str]]:
    return [p for p in rule.code_params if p.get("relationship") == rel and p.get("related_code")]


def build_query(rule, audit_action: str) -> Dict[str, Any]:
    """-> {'sql': str, 'columns': [physical columns used], 'todo': [inputs still needed]}"""
    used: Set[str] = set()
    todo: List[str] = []
    ctes: List[str] = []
    joins: List[str] = []
    extra_select: List[str] = []
    where: List[str] = []
    cat = rule.category
    text = rule.text
    T = CLAIM_TABLE
    c = "c"

    # ---- common skeleton filters --------------------------------------------------
    lob = LOB_VALUES.get(rule.line_of_business)
    if lob:
        where.append(f"{col(c, 'line_of_business', used)} = '{lob}'")
    ctype = CLAIM_TYPE_VALUES.get(rule.billing_class)
    if ctype:
        where.append(f"{col(c, 'claim_type', used)} = '{ctype}'")
    where.append(_active(c, used))
    codes = sorted(rule.codes, key=lambda x: (not x[:1].isdigit(), x))
    if codes:
        where.append(f"{col(c, 'proc_code', used)} IN {in_list(codes)}")
    base_n = len(where)

    neg = is_negative(text)
    hist = (f"SELECT 1 FROM {T} h\n        WHERE {col('h', 'member_id', used)} = {col(c, 'member_id', used)}\n"
            f"          AND {_active('h', used)}\n          AND {_earlier('h', c, used)}")

    # ---- modifiers (any category) ------------------------------------------------
    if rule.mods_required:
        where.append(f"NOT {_any_mod(c, rule.mods_required, used)}  -- required modifier missing")
    if rule.mods_prohibited:
        where.append(f"{_any_mod(c, rule.mods_prohibited, used)}  -- modifier not allowed")

    # ---- bill type (institutional) -----------------------------------------------
    if rule.bill_types:
        bt = " OR ".join(f"{col(c, 'bill_type', used)} LIKE '{b.rstrip('X').lstrip('0') or b}%'" for b in rule.bill_types)
        where.append(f"({bt})" if neg else f"NOT ({bt})")
    elif cat == "BILL_TYPE_REVENUE_CODE":
        todo.append("bill types / revenue codes")
        where.append(f"{col(c, 'bill_type', used)} IN (/* TODO: bill types */ NULL)")

    # ---- category block ----------------------------------------------------------
    span_p = _params(rule, "Time Span")
    fam_p = _params(rule, "Code Family")
    age_p = _params(rule, "Age Range")
    ndc_p = [p for p in rule.code_params if p.get("relationship") == "NDC11"]
    addon_p = _rel(rule, "ADD_ON_TO_PRIMARY")
    comp_p = _rel(rule, "COMPREHENSIVE_TO_COMPONENT")
    freq = parse_frequency(text)

    if cat == "TIME_SPAN_LIMIT" and span_p:
        rows = []
        for p in span_p:
            ts = parse_time_span(p["parameter_value"])
            if ts:
                rows.append((p["code"], ts[0], ts[1]))
        ctes.append(values_cte("time_span", ["code", "span_type", "span_days"], sorted(set(rows))))
        joins.append(f"JOIN time_span ts ON ts.code = {col(c, 'proc_code', used)}")
        hd, cd = col("h", "serv_from_date", used), col(c, "serv_from_date", used)
        if SQL_DIALECT == "sqlite":
            win = f"julianday({hd}) > julianday({cd}) - ts.span_days"
        elif SQL_DIALECT in ("sqlserver", "snowflake"):
            win = f"{hd} > DATEADD(day, -ts.span_days, {cd})"
        elif SQL_DIALECT == "postgres":
            win = f"{hd} > {cd} - ts.span_days"
        else:
            win = f"{hd} > {cd} - ts.span_days"
        where.append(
            f"EXISTS (\n        {hist}\n          AND {col('h', 'proc_code', used)} = {col(c, 'proc_code', used)}\n"
            f"          AND {col('h', 'billing_tin', used)} = {col(c, 'billing_tin', used)}  -- same group physician\n"
            f"          AND ((ts.span_type = 'DAYS' AND {win})\n"
            f"            OR (ts.span_type = 'CALENDAR_MONTH' AND {same_period(hd, cd, 'month')})\n"
            f"            OR (ts.span_type = 'CALENDAR_YEAR' AND {same_period(hd, cd, 'year')}))\n    )")

    elif cat == "ONCE_IN_A_LIFETIME" and fam_p:
        ctes.append(values_cte("code_family", ["code", "family"], sorted({(p["code"], p["parameter_value"]) for p in fam_p})))
        joins.append(f"JOIN code_family f1 ON f1.code = {col(c, 'proc_code', used)}")
        where.append(f"EXISTS (\n        {hist}\n          AND {col('h', 'proc_code', used)} IN "
                     f"(SELECT f2.code FROM code_family f2 WHERE f2.family = f1.family)\n    )")

    elif cat == "ADD_ON_PRIMARY":
        if addon_p:
            ctes.append(values_cte("addon_primary", ["addon_code", "primary_code"],
                                   sorted({(p["code"], p["related_code"]) for p in addon_p})))
            prim = f"(SELECT ap.primary_code FROM addon_primary ap WHERE ap.addon_code = {col(c, 'proc_code', used)})"
        else:
            todo.append("eligible primary codes for the add-on codes")
            prim = "(/* TODO: eligible primary codes */ NULL)"
        where.append(
            f"NOT EXISTS (\n        SELECT 1 FROM {T} p\n"
            f"        WHERE {col('p', 'member_id', used)} = {col(c, 'member_id', used)}\n"
            f"          AND {col('p', 'serv_from_date', used)} = {col(c, 'serv_from_date', used)}\n"
            f"          AND {col('p', 'billing_tin', used)} = {col(c, 'billing_tin', used)}\n"
            f"          AND {_active('p', used)}\n"
            f"          AND {col('p', 'proc_code', used)} IN {prim}\n    )  -- no eligible primary code on the same date")

    elif cat == "BUNDLING_INCLUSION" and comp_p:
        ctes.append(values_cte("comp_component", ["comprehensive_code", "component_code"],
                               sorted({(p["code"], p["related_code"]) for p in comp_p})))
        where.append(
            f"EXISTS (\n        SELECT 1 FROM {T} o JOIN comp_component cc ON cc.comprehensive_code = {col('o', 'proc_code', used)}\n"
            f"        WHERE cc.component_code = {col(c, 'proc_code', used)}\n"
            f"          AND {col('o', 'member_id', used)} = {col(c, 'member_id', used)}\n"
            f"          AND {col('o', 'billing_tin', used)} = {col(c, 'billing_tin', used)}\n"
            f"          AND {col('o', 'serv_from_date', used)} = {col(c, 'serv_from_date', used)}\n"
            f"          AND {_active('o', used)}\n    )  -- component billed with its comprehensive code")

    elif cat == "BUNDLING_INCLUSION":
        if len(codes) >= 2:
            where.append(
                f"EXISTS (\n        SELECT 1 FROM {T} o\n"
                f"        WHERE {col('o', 'member_id', used)} = {col(c, 'member_id', used)}\n"
                f"          AND {col('o', 'billing_tin', used)} = {col(c, 'billing_tin', used)}\n"
                f"          AND {col('o', 'serv_from_date', used)} = {col(c, 'serv_from_date', used)}\n"
                f"          AND {_active('o', used)}\n"
                f"          AND {col('o', 'proc_code', used)} IN {in_list(codes)}\n"
                f"          AND {col('o', 'proc_code', used)} <> {col(c, 'proc_code', used)}\n    )  -- billed together on the same date")
        else:
            todo.append("the primary / comprehensive codes this service is bundled into")
            where.append(
                f"EXISTS (\n        SELECT 1 FROM {T} o\n"
                f"        WHERE {col('o', 'member_id', used)} = {col(c, 'member_id', used)}\n"
                f"          AND {col('o', 'serv_from_date', used)} = {col(c, 'serv_from_date', used)}\n"
                f"          AND {_active('o', used)}\n"
                f"          AND {col('o', 'proc_code', used)} IN (/* TODO: primary codes */ NULL)\n    )")

    elif age_p and cat != "TIME_SPAN_LIMIT":
        # per-code age ranges from the Age-Based attachment
        age_rows = []
        for p in age_p:
            rng = parse_age_param(p["parameter_value"])
            if rng:
                age_rows.append((p["code"], rng[0], rng[1]))
        if age_rows:
            age_expr = age_in_days(col(c, "member_dob", used), col(c, "serv_from_date", used))
            ctes.append(values_cte("age_range", ["code", "min_age_days", "max_age_days"], sorted(set(age_rows))))
            joins.append(f"JOIN age_range ar ON ar.code = {col(c, 'proc_code', used)}")
            where.append(f"{age_expr} NOT BETWEEN ar.min_age_days AND ar.max_age_days  -- age outside the code's range")

    elif cat == "PRIOR_AUTHORIZATION":
        where.append(f"{col(c, 'auth_number', used)} IS NULL  -- no authorization on the line")

    elif cat == "PLACE_OF_SERVICE":
        pos = parse_pos(text)
        if pos:
            op = "IN" if neg else "NOT IN"
            where.append(f"{col(c, 'place_of_service', used)} {op} {in_list(pos)}")
        else:
            todo.append("place of service codes")
            where.append(f"{col(c, 'place_of_service', used)} NOT IN (/* TODO: allowed POS */ NULL)")

    # age / gender: any rule that is written for a population ("individuals 18 years of age or older",
    # "younger than 18", "in males") gets a member_dob / member_gender check.
    #   coverage rule  -> flag lines where the member is OUTSIDE the population
    #   denial rule    -> flag lines where the member is INSIDE the population
    if not age_p:
        pop, pop_desc = age_gender_population(text, c, used)
        if pop:
            where.append(f"({pop})  -- member is {pop_desc}: not covered" if neg
                         else f"NOT ({pop})  -- member outside: {pop_desc}")
        elif cat == "AGE_GENDER_RESTRICTION":
            todo.append("allowed age range / gender (several age brackets or no age stated)")

    # frequency: categories with a frequency in the text, NDC limits, and time span / lifetime without attachment
    if ndc_p:
        rows = []
        for p in ndc_p:
            m = re.match(r"\s*([\d.]+)", p.get("parameter_value") or "")
            if m:
                rows.append((p["code"], p["related_code"], float(m.group(1))))
        if rows:
            ctes.append(values_cte("ndc_limit", ["code", "ndc", "max_units"], sorted(set(rows))))
            joins.append(f"JOIN ndc_limit nl ON nl.code = {col(c, 'proc_code', used)} AND nl.ndc = {col(c, 'ndc_code', used)}")
            where.append(f"{col(c, 'ndc_units', used)} > nl.max_units  -- more units than the NDC package allows")
    elif (cat in ("FREQUENCY_LIMIT", "TIME_SPAN_LIMIT", "ONCE_IN_A_LIFETIME") and not (span_p or fam_p)) or \
            (freq and cat in ("REIMBURSEMENT_CRITERIA", "AGE_GENDER_RESTRICTION", "DENIAL_PROHIBITION") and not neg):
        if freq:
            same = f"\n          AND {col('h', 'proc_code', used)} = {col(c, 'proc_code', used)}"
            hd, cd = col("h", "serv_from_date", used), col(c, "serv_from_date", used)
            k = freq["kind"]
            if k == "lifetime":
                where.append(f"EXISTS (\n        {hist}{same}\n    )  -- already billed once in the member's lifetime")
            elif k == "interval":
                where.append(f"EXISTS (\n        {hist}{same}\n          AND {hd} > {date_minus(cd, freq['n'], freq['unit'])}\n"
                             f"    )  -- billed again within {freq['n']} {freq['unit']}(s)")
            elif k == "calendar":
                where.append(f"EXISTS (\n        {hist}{same}\n          AND {same_period(hd, cd, freq['unit'])}\n"
                             f"    )  -- more than once in the same calendar {freq['unit']}")
            elif k == "per_day":
                where.append(f"({col(c, 'units_billed', used)} > 1 OR EXISTS (\n        {hist}{same}\n"
                             f"          AND {hd} = {cd}\n    ))  -- more than once per date of service")
            elif k == "max_units":
                where.append(f"{col(c, 'units_billed', used)} > {freq['n']}  -- more units than allowed")
        elif cat in ("FREQUENCY_LIMIT", "TIME_SPAN_LIMIT", "ONCE_IN_A_LIFETIME"):
            todo.append("frequency limit (could not be read from the rule text)")
            where.append(f"{col(c, 'units_billed', used)} > /* TODO: max allowed units */ NULL")

    # diagnosis: coverage rules list conditions; denial rules list excluded conditions
    icd = parse_icd(text)
    if not icd:                      # policy tables can attach an explicit ICD-10 list to the rule
        icd = [x for x in ((getattr(rule, "extra", None) or {}).get("icd_codes") or []) if x]
        icd = list(dict.fromkeys((x if ICD_STORED_WITH_DOT else x.replace(".", "")).upper() for x in icd))
    icd_found: List[str] = []
    if icd:
        icd_found = list(icd)        # explicit lists are shown in Possible_icd_codes too
        icd_neg = neg
        if REQUIRED_DX_POSITIVE and neg and _DX_REQUIRED_RE.search(text) and not _DX_EXCLUDING_RE.search(text):
            icd_neg = False         # the listed diagnoses are what the claim MUST carry
        where.append(f"{_any_dx(c, icd, used)}  -- excluded diagnosis" if icd_neg
                     else f"NOT {_any_dx(c, icd, used)}  -- none of the required / covered diagnoses")
    elif cat == "DIAGNOSIS_REQUIREMENT" or (
            cat == "REIMBURSEMENT_CRITERIA" and not neg and
            re.search(r"\b(?:conditions?|indications?|diagnos[ie]s|individuals? with|for (?:the )?(?:evaluation|monitoring)|"
                      r"symptom|suspected|history of|disorder|disease|syndrome)", text, re.I)):
        mapped = _lookup_icd(rule)
        if mapped:
            icd_found = list(dict.fromkeys(x for _, cs in mapped for x in cs))
            conds = "; ".join(cond for cond, _ in mapped)[:120]
            where.append(f"NOT {_any_dx(c, icd_found, used)}  -- ICD-10 (UMLS) for: {conds} - verify")
        else:
            todo.append("ICD-10 codes for the conditions named in the rule")
            where.append(f"NOT {_any_dx(c, ['/* TODO: covered ICD-10 codes */'], used)}".replace("'/* TODO: covered ICD-10 codes */'",
                                                                                               "/* TODO: covered ICD-10 codes */ NULL"))

    # payment adjustment: expected percentage shown on the result
    if cat == "PAYMENT_ADJUSTMENT":
        pct = parse_percent(text)
        if len(pct) == 1:
            extra_select.append(f"{pct[0]} AS expected_payment_pct")
            where.append(f"{col(c, 'paid_amt', used)} > {col(c, 'allowed_amt', used)} * {float(pct[0]) / 100:g}"
                         f"  -- paid above {pct[0]}%")
        else:
            todo.append("payment percentage" + (f" (rule states {' / '.join(p + '%' for p in pct)})" if pct else ""))
            where.append(f"{col(c, 'paid_amt', used)} > {col(c, 'allowed_amt', used)} * /* TODO: payment percentage */ NULL")
    if cat == "MODIFIER_RULE" and not (rule.mods_required or rule.mods_prohibited):
        if rule.modifiers:
            where.append(f"{_any_mod(c, rule.modifiers, used)}  -- lines carrying the rule's modifiers (review)")

    # scope (AND) vs violation checks (OR): a line breaks the rule if ANY check fails
    scope, viol = where[:base_n], where[base_n:]
    header_note = ""
    if not codes:
        if any("TODO" not in w for w in viol):
            header_note = "-- Applies to all procedure codes (the rule names no specific codes)"
        else:
            todo.insert(0, "procedure codes in scope (the rule names no codes)")
            scope.append(f"{col(c, 'proc_code', used)} IN (/* TODO: codes in scope */ NULL)")
    if not viol and not neg and cat not in ("DENIAL_PROHIBITION",):
        # a coverage / modifier / exception rule whose criteria could not be codified:
        # never return every paid line by default
        todo.append({"EXCEPTION_OVERRIDE": "exception condition",
                     "MODIFIER_RULE": "modifier condition"}.get(cat, "the rule's criteria"))
        viol.append("1 = 0  /* TODO: replace with the rule's criteria */")
    if len(viol) > 1:
        where = scope + ["(\n        " + "\n     OR ".join(v.replace("\n", "\n   ") for v in viol) + "\n  )"]
    else:
        where = scope + viol

    # ---- assemble -----------------------------------------------------------------
    sel = [col(c, x, used) for x in ("claim_id", "line_num", "member_id", "proc_code", "modifier_1",
                                     "serv_from_date", "units_billed", "paid_amt")]
    sel += [f"'{rule.rule_id}' AS rule_id", f"'{audit_action}' AS audit_action"] + extra_select
    summary = re.sub(r"\s+", " ", text)[:160].replace("*/", "* /")
    header = [f"-- {rule.rule_id} | {cat} | {audit_action}",
              f"-- Rule: {summary}{'...' if len(text) > 160 else ''}"]
    if header_note:
        header.append(header_note)
    if cat == "EXCEPTION_OVERRIDE":
        header.append("-- EXCEPTION rule: lines returned here meet the exception and should be EXCLUDED from the base edit")
    if todo:
        header.append("-- NEEDS INPUT: " + "; ".join(dict.fromkeys(todo)))
    sql = "\n".join(header) + "\n"
    if ctes:
        sql += "WITH " + ",\n".join(ctes) + "\n"
    sql += "SELECT " + ",\n       ".join(sel) + f"\nFROM {T} c\n"
    if joins:
        sql += "\n".join(joins) + "\n"
    sql += "WHERE " + "\n  AND ".join(where) + "\n;"   # ; on its own line so a trailing -- comment cannot swallow it
    phys = [COLUMNS[k] for k in COLUMNS if k in used]
    return {"sql": sql, "columns": phys, "todo": list(dict.fromkeys(todo)), "icd_codes": icd_found}


def file_header() -> str:
    return (f"-- Billing rule queries  |  dialect: {SQL_DIALECT}  |  table: {CLAIM_TABLE}\n"
            "-- Each query returns the claim lines that break its rule.\n"
            "-- Lines marked 'NEEDS INPUT' contain /* TODO */ placeholders that must be filled before running.\n\n"
            + table_ddl() + "\n\n")
