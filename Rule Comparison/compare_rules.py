#!/usr/bin/env python3
"""
compare_rules.py
================
CPT-code-level comparison of billing rules between payers.

Input  : billing_rule_uhc.xlsx, billing_rule_humana.xlsx, billing_rule_devoted.xlsx
         (sheet "Rule_Detail" of each - one row per rule, never clubbed)
Output : comparison_report.xlsx
           Summary                  CPT-code counts per pair: shared / only-one-client codes and results
           CPT_Comparison           one row per CPT code x Client1 rule x Client2 rule that both list that code
                                    (codes written in the policies)
           CPT_Comparison_Mapped    the same, where at least one of the two rules only has codes mapped at policy
                                    level (CPT_CODES_USED / mapped_cpt_codes) - kept apart, treat as indicative
           CPT_Only_One_Client      one row per CPT code x rule where the code has a rule at only one client of the pair
           Rules_Without_Codes      rules that list no CPT/HCPCS code (cannot be compared by code), once per payer
           Read_Me                  what every column and value means

Nothing is clubbed: a rule with 10 codes gives 10 rows. Every (rule, code) of every pair is either on a comparison
sheet or on CPT_Only_One_Client, and every rule without codes is on Rules_Without_Codes - the script checks this.

Pairs (Client1 vs Client2) are compared once each, never mirrored (see PAIRS):
    UHC vs Humana, UHC vs Devoted, Humana vs Devoted

Usage
  python compare_rules.py                                  # workbooks next to this script
  python compare_rules.py --dir "C:\\...\\Rule Comparison"
  python compare_rules.py --uhc a.xlsx --humana b.xlsx --devoted c.xlsx --out comparison_report.xlsx
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ------------------------------------------------------------------------------
# settings
# ------------------------------------------------------------------------------
PAYER_KEYS = ["UHC", "Humana", "Devoted"]
# (Client1, Client2) - each pair once
PAIRS = [("UHC", "Humana"), ("UHC", "Devoted"), ("Humana", "Devoted")]
FILE_PATTERNS = {"UHC": ["billing_rule_uhc.xlsx", "billing_rules_uhc.xlsx"],
                 "Humana": ["billing_rule_humana.xlsx", "billing_rules_humana.xlsx"],
                 "Devoted": ["billing_rule_devoted.xlsx", "billing_rules_devoted.xlsx"]}
SHEET_CANDIDATES = ("Rule_Detail", "Rules")
MAPPED_SOURCE_RE = re.compile(r"policy scope", re.I)     # Code_Source of codes that come from the policy-level mapping
MAX_RANGE = 500                                          # a code range wider than this is kept as its two endpoints
MAX_CELL = 32000                                         # Excel cell limit is 32,767 characters

# SQL comments that are not edit checks (diagnosis lists are compared under "Conditions")
NOT_EDIT_CHECK_RE = re.compile(r"TODO|diagnos|ICD-10", re.I)

NEG_OUTCOME_RE = re.compile(
    r"\b(?:not|never|no longer)\s+(?:be\s+)?(?:separately\s+)?(?:reimburs\w*|covered|payable|allowed|eligible|paid)\b|"
    r"\bnon-?(?:covered|reimbursable|payable)\b|\bdenied\b|\bwill deny\b|"
    r"\b(?:do|does|will)\s+not\s+(?:pay|reimburse|cover|allow)\b", re.I)
POS_OUTCOME_RE = re.compile(r"\b(?:reimburs\w*|covers?|covered|allows?|allowed|pays?|payable|"
                            r"eligible for (?:payment|reimbursement))\b", re.I)

RESULTS = ["POSSIBLE_CONFLICT", "SAME_RULE", "SAME_CODES_DIFFERENT_RULE_DETAILS", "DIFFERENT_DETAILS"]

COMPARISON_COLUMNS = [
    ("Client1", 12), ("Client2", 12), ("CPT_Code", 11), ("Comparison_Result", 22), ("Differences", 55),
    ("Client1_Rule_ID", 17), ("Client2_Rule_ID", 17), ("Client1_Policy_Title", 30), ("Client2_Policy_Title", 30),
    ("Client1_Category", 18), ("Client2_Category", 18), ("Client1_Audit_Action", 22), ("Client2_Audit_Action", 22),
    ("Client1_Rule_Text", 70), ("Client2_Rule_Text", 70), ("Code_Match_Type", 17), ("Rules_Shared_Code_Count", 14),
    ("Client1_Code_Count", 13), ("Client2_Code_Count", 13), ("Code_Overlap_Pct", 12),
    ("Pct_Of_Client1_Codes_Shared", 14), ("Pct_Of_Client2_Codes_Shared", 14), ("Client1_Edit_Checks", 32),
    ("Client2_Edit_Checks", 32), ("Client1_ICD_Codes", 24), ("Client2_ICD_Codes", 24), ("Client1_LOB", 13),
    ("Client2_LOB", 13), ("Client1_Policy_Number", 16), ("Client2_Policy_Number", 16), ("Client1_Code_Source", 20),
    ("Client2_Code_Source", 20),
]
ONLY_ONE_COLUMNS = [
    ("Client1", 12), ("Client2", 12), ("CPT_Code", 11), ("Has_Rule", 16), ("No_Rule_In", 16), ("Gap_Severity", 12),
    ("Rule_ID", 20), ("Policy_Title", 32), ("Policy_Number", 15), ("Rule_Category", 22), ("Audit_Action", 22),
    ("Policy_Rule_Text", 80), ("Edit_Checks", 32), ("Possible_icd_codes", 24), ("Line_of_Business", 18),
    ("Code_Source", 20),
]
NO_CODE_COLUMNS = [
    ("Client", 16), ("Rule_ID", 20), ("Policy_Title", 32), ("Policy_Number", 15), ("Rule_Category", 22),
    ("Audit_Action", 22), ("Policy_Rule_Text", 80), ("Edit_Checks", 32), ("Line_of_Business", 18), ("Source_PDF", 30),
    ("Reason", 40),
]
WRAP_COLS = {"Differences", "Client1_Policy_Title", "Client2_Policy_Title", "Client1_Rule_Text", "Client2_Rule_Text",
             "Client1_Edit_Checks", "Client2_Edit_Checks", "Policy_Title", "Policy_Rule_Text", "Edit_Checks", "Reason",
             "Metric"}
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
RESULT_FILL = {"POSSIBLE_CONFLICT": "F8CBAD", "SAME_RULE": "C6E0B4", "SAME_CODES_DIFFERENT_RULE_DETAILS": "FFE699",
               "DIFFERENT_DETAILS": "DDEBF7"}
GAP_TEXT_CHARS = 1500       # rule text shown on CPT_Only_One_Client (one rule repeats on every code it lists)


# ------------------------------------------------------------------------------
# code helpers
# ------------------------------------------------------------------------------
_CODE_RE = re.compile(r"^([A-Z]?)(\d{3,5})([A-Z]?)$")


def _split(code: str) -> Optional[Tuple[str, int, str, int]]:
    m = _CODE_RE.match(code)
    return (m.group(1), int(m.group(2)), m.group(3), len(m.group(2))) if m else None


def code_key(code: str) -> Tuple:
    p = _split(code)
    return (0, p[0], p[2], p[1]) if p else (1, code, "", 0)


def expand_codes(text: str) -> List[str]:
    """'99281-99285, G0378-G0384, 0024U' -> single codes (order kept, no duplicates)."""
    out: "OrderedDict[str, None]" = OrderedDict()
    for tok in re.split(r"[,;\s]+", (text or "").upper()):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^([A-Z0-9]+)\s*[-–]\s*([A-Z0-9]+)$", tok)
        if m:
            a, b = _split(m.group(1)), _split(m.group(2))
            if a and b and a[0] == b[0] and a[2] == b[2] and a[1] <= b[1] and b[1] - a[1] < MAX_RANGE:
                for n in range(a[1], b[1] + 1):
                    out[f"{a[0]}{n:0{a[3]}d}{a[2]}"] = None
                continue
            out[m.group(1)] = None
            out[m.group(2)] = None
            continue
        out[tok] = None
    return list(out)


def compress_codes(codes: Iterable[str]) -> str:
    """Sorted codes with runs of 3+ consecutive codes written as a range."""
    codes = sorted(set(codes), key=code_key)
    parts: List[str] = []
    i = 0
    while i < len(codes):
        j = i
        while j + 1 < len(codes):
            b, c = _split(codes[j + 1]), _split(codes[j])
            if b and c and b[0] == c[0] and b[2] == c[2] and b[3] == c[3] and b[1] == c[1] + 1:
                j += 1
            else:
                break
        parts.append(f"{codes[i]}-{codes[j]}" if j - i >= 2 else ", ".join(codes[i:j + 1]))
        i = j + 1
    return ", ".join(parts)


def pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


# ------------------------------------------------------------------------------
# loading
# ------------------------------------------------------------------------------
def s(v) -> str:
    return "" if v is None else str(v).strip()


def split_list(v) -> Set[str]:
    return {x.strip() for x in re.split(r"[,;]\s*", s(v)) if x.strip()}


def edit_checks(sql: str) -> List[str]:
    """The checks a rule's SQL applies, taken from the comment at the end of each WHERE line."""
    out: "OrderedDict[str, None]" = OrderedDict()
    for line in (sql or "").split("\n"):
        if line.lstrip().startswith("--") or "--" not in line:
            continue
        txt = line.split("--", 1)[1].strip()
        if txt and not NOT_EDIT_CHECK_RE.search(txt):
            out[txt] = None
    return list(out)


def outcome(rule: Dict) -> str:
    """'denies' / 'pays' / '' - what the rule does with the codes, from its audit action and wording."""
    act, text = rule["action"], rule["text"]
    if act == "DENY_LINE" or NEG_OUTCOME_RE.search(text):
        return "denies"
    if act == "PAY_IF_CRITERIA_MET" and POS_OUTCOME_RE.search(text):
        return "pays"
    return ""


def load_rules(path: Path, key: str) -> List[Dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    sheet = next((n for n in SHEET_CANDIDATES if n in wb.sheetnames), None)
    if sheet is None:
        raise SystemExit(f"{path.name}: none of the sheets {SHEET_CANDIDATES} found")
    rows = list(wb[sheet].iter_rows(values_only=True))
    header = [s(h) for h in rows[0]]
    need = {"Rule_ID", "Policy_Title", "Rule_Category", "Target_Codes"}
    if not need <= set(header):
        raise SystemExit(f"{path.name}: sheet {sheet} lacks columns {sorted(need - set(header))}")
    rules = []
    for order, r in enumerate(rows[1:]):
        d = dict(zip(header, r))
        if not s(d.get("Rule_ID")):
            continue
        text = s(d.get("Policy_Rule_Text"))
        rule = {
            "key": key, "payer": s(d.get("Payer")) or key, "order": order,
            "id": s(d["Rule_ID"]), "policy": s(d["Policy_Title"]), "policy_number": s(d.get("Policy_Number")),
            "lob": s(d.get("Line_of_Business")), "category": s(d["Rule_Category"]) or "UNCATEGORIZED",
            "action": s(d.get("Audit_Action")), "text": text,
            "codes": expand_codes(s(d.get("Target_Codes"))), "target_codes": s(d.get("Target_Codes")),
            "basis": "Mapped (policy level)" if MAPPED_SOURCE_RE.search(s(d.get("Code_Source"))) else "From policy text",
            "req_mods": split_list(d.get("Modifiers_Required")), "pro_mods": split_list(d.get("Modifiers_Prohibited")),
            "icd": s(d.get("Possible_icd_codes")), "checks": edit_checks(s(d.get("SQL_Query"))),
            "percents": list(OrderedDict.fromkeys(re.findall(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", text))),
            "pdf": s(d.get("Source_PDF")),
        }
        rule["code_set"] = set(rule["codes"])
        rule["outcome"] = outcome(rule)
        rules.append(rule)
    return rules


def find_inputs(args) -> Dict[str, Path]:
    base = Path(args.dir) if args.dir else Path(__file__).resolve().parent
    found: Dict[str, Path] = {}
    for key in PAYER_KEYS:
        explicit = getattr(args, key.lower())
        if explicit:
            found[key] = Path(explicit)
            continue
        hit = next((base / n for n in FILE_PATTERNS[key] if (base / n).exists()), None)
        if hit is None:
            raise SystemExit(f"{key}: no workbook found in {base} (looked for {', '.join(FILE_PATTERNS[key])})")
        found[key] = hit
    return found


# ------------------------------------------------------------------------------
# comparison
# ------------------------------------------------------------------------------
def match_type(a: Set[str], b: Set[str]) -> str:
    if a == b:
        return "SAME_CODES"
    if a < b:
        return "CLIENT1_CODES_WITHIN_CLIENT2"
    if b < a:
        return "CLIENT2_CODES_WITHIN_CLIENT1"
    return "PARTIAL_OVERLAP"


def differences(r1: Dict, r2: Dict, shared: Set[str], only1: Set[str], only2: Set[str]) -> Tuple[List[str], bool, bool]:
    """Return (difference lines, outcome conflict?, any difference other than line of business / codes?)."""
    n1, n2 = r1["payer"], r2["payer"]
    lines: List[str] = []
    conflict = bool(r1["outcome"] and r2["outcome"] and r1["outcome"] != r2["outcome"])
    if conflict:
        lines.append(f"Outcome: {n1} {r1['outcome']}, {n2} {r2['outcome']} the shared codes")
    detail = conflict
    if r1["category"] != r2["category"]:
        lines.append(f"Category: {n1} {r1['category']} vs {n2} {r2['category']}")
        detail = True
    if r1["action"] != r2["action"]:
        lines.append(f"Audit action: {n1} {r1['action'] or 'none'} vs {n2} {r2['action'] or 'none'}")
        detail = True
    if r1["lob"] != r2["lob"]:
        lines.append(f"Line of business: {n1} {r1['lob']} vs {n2} {r2['lob']}")
    if only1 or only2:
        lines.append(f"Codes: {len(shared)} shared, {len(only1)} only in {n1}, {len(only2)} only in {n2}")
    if bool(r1["icd"]) != bool(r2["icd"]):
        lines.append(f"Conditions: only {n1 if r1['icd'] else n2} lists ICD-10 codes")
        detail = True
    elif r1["icd"] and r1["icd"] != r2["icd"]:
        lines.append(f"Conditions: {n1} and {n2} list different ICD-10 codes")
        detail = True
    c1, c2 = set(r1["checks"]), set(r2["checks"])
    if c1 - c2:
        lines.append(f"Edit checks only in {n1}: " + "; ".join(x for x in r1["checks"] if x not in c2))
        detail = True
    if c2 - c1:
        lines.append(f"Edit checks only in {n2}: " + "; ".join(x for x in r2["checks"] if x not in c1))
        detail = True
    if set(r1["percents"]) != set(r2["percents"]):
        fmt = lambda r: ", ".join(p + "%" for p in r["percents"]) or "none"  # noqa: E731
        lines.append(f"Percentages: {n1} {fmt(r1)} vs {n2} {fmt(r2)}")
        detail = True
    for label, k in (("Required modifiers", "req_mods"), ("Prohibited modifiers", "pro_mods")):
        if r1[k] != r2[k]:
            lines.append(f"{label}: {n1} {', '.join(sorted(r1[k])) or 'none'} vs {n2} {', '.join(sorted(r2[k])) or 'none'}")
            detail = True
    if not lines:
        lines.append("No differences found")
    return lines, conflict, detail


def compare_pair(rules1: List[Dict], rules2: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Return (comparison rows, only-one-client rows) for one pair, one row per CPT code."""
    by_code2: Dict[str, List[Dict]] = defaultdict(list)
    for r in rules2:
        for c in r["codes"]:
            by_code2[c].append(r)
    codes1 = {c for r in rules1 for c in r["code_set"]}
    name1 = rules1[0]["payer"] if rules1 else ""
    name2 = rules2[0]["payer"] if rules2 else ""
    pair_cache: Dict[Tuple[str, str], Dict] = {}
    rows: List[Dict] = []
    gaps: List[Dict] = []

    def pair_info(r1: Dict, r2: Dict) -> Dict:
        key = (r1["id"], r2["id"])
        if key not in pair_cache:
            shared = r1["code_set"] & r2["code_set"]
            only1, only2 = r1["code_set"] - shared, r2["code_set"] - shared
            mt = match_type(r1["code_set"], r2["code_set"])
            lines, conflict, detail = differences(r1, r2, shared, only1, only2)
            if conflict:
                result = "POSSIBLE_CONFLICT"
            elif mt == "SAME_CODES":
                result = "SAME_CODES_DIFFERENT_RULE_DETAILS" if detail else "SAME_RULE"
            else:
                result = "DIFFERENT_DETAILS"
            info = {
                "Client1": r1["payer"], "Client2": r2["payer"], "Comparison_Result": result,
                "Differences": "\n".join(lines), "Code_Match_Type": mt, "Rules_Shared_Code_Count": len(shared),
                "Code_Overlap_Pct": pct(len(shared), len(r1["code_set"] | r2["code_set"])),
                "Pct_Of_Client1_Codes_Shared": pct(len(shared), len(r1["code_set"])),
                "Pct_Of_Client2_Codes_Shared": pct(len(shared), len(r2["code_set"])),
                "_mapped": "Mapped (policy level)" in (r1["basis"], r2["basis"]),
            }
            for side, r in (("Client1", r1), ("Client2", r2)):
                info.update({
                    f"{side}_Rule_ID": r["id"], f"{side}_Policy_Title": r["policy"], f"{side}_Category": r["category"],
                    f"{side}_Audit_Action": r["action"], f"{side}_Rule_Text": r["text"][:MAX_CELL],
                    f"{side}_Code_Count": len(r["code_set"]), f"{side}_Edit_Checks": "\n".join(r["checks"]),
                    f"{side}_ICD_Codes": r["icd"][:MAX_CELL], f"{side}_LOB": r["lob"],
                    f"{side}_Policy_Number": r["policy_number"], f"{side}_Code_Source": r["basis"],
                })
            pair_cache[key] = info
        return pair_cache[key]

    for r1 in rules1:
        for code in sorted(r1["code_set"], key=code_key):
            partners = by_code2.get(code)
            if not partners:
                gaps.append(gap_row(r1, code, c1=r1["payer"], c2=name2, missing=name2, side=0))
                continue
            for r2 in partners:
                rows.append({**pair_info(r1, r2), "CPT_Code": code, "_key": (code_key(code), r1["order"], r2["order"])})
    for r2 in rules2:
        for code in sorted(r2["code_set"], key=code_key):
            if code not in codes1:
                gaps.append(gap_row(r2, code, c1=name1, c2=r2["payer"], missing=name1, side=1))
    return rows, gaps


def gap_row(r: Dict, code: str, c1: str, c2: str, missing: str, side: int) -> Dict:
    """One CPT code that only one client of the pair has a rule for."""
    return {
        "Client1": c1, "Client2": c2, "CPT_Code": code, "Has_Rule": r["payer"], "No_Rule_In": missing,
        "Gap_Severity": "HIGH" if r["action"].startswith("DENY") else "MEDIUM",
        "Rule_ID": r["id"], "Policy_Title": r["policy"], "Policy_Number": r["policy_number"],
        "Rule_Category": r["category"], "Audit_Action": r["action"],
        "Policy_Rule_Text": r["text"][:GAP_TEXT_CHARS] + ("..." if len(r["text"]) > GAP_TEXT_CHARS else ""),
        "Edit_Checks": "\n".join(r["checks"]), "Possible_icd_codes": r["icd"][:2000], "Line_of_Business": r["lob"],
        "Code_Source": r["basis"], "_key": (code_key(code), side, r["order"]),
    }


def no_code_rows(rules: List[Dict]) -> List[Dict]:
    return [{"Client": r["payer"], "Rule_ID": r["id"], "Policy_Title": r["policy"], "Policy_Number": r["policy_number"],
             "Rule_Category": r["category"], "Audit_Action": r["action"], "Policy_Rule_Text": r["text"][:MAX_CELL],
             "Edit_Checks": "\n".join(r["checks"]), "Line_of_Business": r["lob"], "Source_PDF": r["pdf"],
             "Reason": "Rule names no CPT/HCPCS codes (policy-wide rule), so it cannot be compared by code"}
            for r in rules if not r["code_set"]]


def integrity_check(rules: Dict[str, List[Dict]], names: Dict[str, str], comp: List[Dict], gaps: List[Dict],
                    no_code: List[Dict]) -> None:
    """Nothing missing, nothing repeated, nothing mirrored."""
    keys = Counter((r["Client1"], r["Client2"], r["CPT_Code"], r["Client1_Rule_ID"], r["Client2_Rule_ID"]) for r in comp)
    repeated = [k for k, v in keys.items() if v > 1]
    mirrored = [k for k in keys if (k[1], k[0], k[2], k[4], k[3]) in keys]
    covered = set()
    for r in comp:
        covered.add((r["Client1"], r["Client2"], r["Client1"], r["Client1_Rule_ID"], r["CPT_Code"]))
        covered.add((r["Client1"], r["Client2"], r["Client2"], r["Client2_Rule_ID"], r["CPT_Code"]))
    for g in gaps:
        covered.add((g["Client1"], g["Client2"], g["Has_Rule"], g["Rule_ID"], g["CPT_Code"]))
    expected = {(names[a], names[b], names[k], r["id"], c) for a, b in PAIRS for k in (a, b) for r in rules[k]
                for c in r["code_set"]}
    missing = expected - covered
    no_code_ids = {(r["Client"], r["Rule_ID"]) for r in no_code}
    missing_rules = [(r["payer"], r["id"]) for rs in rules.values() for r in rs
                     if not r["code_set"] and (r["payer"], r["id"]) not in no_code_ids]
    if repeated or mirrored or missing or missing_rules:
        raise SystemExit(f"integrity check failed: {len(repeated)} repeated, {len(mirrored)} mirrored, "
                         f"{len(missing)} (rule, code) pairs missing, {len(missing_rules)} rules without codes missing")


# ------------------------------------------------------------------------------
# summary
# ------------------------------------------------------------------------------
def code_status(results: Set[str]) -> str:
    """One status per CPT code and pair: the most important result among its rule pairs."""
    for r in RESULTS:
        if r in results:
            return r
    return ""


def summary_tables(names: Dict[str, str], rules: Dict[str, List[Dict]], explicit: List[Dict], mapped: List[Dict],
                   gaps: List[Dict]) -> List[Tuple[str, List[str], List[List]]]:
    tables = []
    # 1. rules
    rows = []
    for k in PAYER_KEYS:
        rs = rules[k]
        coded = [r for r in rs if r["code_set"]]
        rows.append([names[k], len(rs), sum(r["basis"] == "From policy text" for r in coded),
                     sum(r["basis"] != "From policy text" for r in coded), len(rs) - len(coded),
                     len({c for r in coded for c in r["code_set"]})])
    tables.append(("Rules and CPT codes per payer",
                   ["Payer", "Total rules", "Rules with policy codes", "Rules with mapped codes", "Rules without codes",
                    "Distinct CPT codes"], rows))
    # 2. codes per pair
    rows = []
    for a, b in PAIRS:
        ca = {c for r in rules[a] for c in r["code_set"]}
        cb = {c for r in rules[b] for c in r["code_set"]}
        rows.append([names[a], names[b], len(ca), len(cb), len(ca & cb), len(ca - cb), len(cb - ca), len(ca | cb)])
    tables.append(("CPT code coverage per pair",
                   ["Client1", "Client2", "Client1 codes", "Client2 codes", "Codes at both (compared)",
                    "Codes only at Client1", "Codes only at Client2", "All codes"], rows))
    # all codes compared, both sheets together: ONE status per code and pair
    rows = []
    for a, b in PAIRS:
        per_code: Dict[str, Set[str]] = defaultdict(set)
        for r in explicit + mapped:
            if (r["Client1"], r["Client2"]) == (names[a], names[b]):
                per_code[r["CPT_Code"]].add(r["Comparison_Result"])
        st = Counter(code_status(v) for v in per_code.values())
        rows.append([names[a], names[b]] + [st.get(x, 0) for x in RESULTS] + [len(per_code)])
    tables.append(("CPT codes by result - ALL compared codes (policy + mapped), one result per code. A code with "
                   "several rule pairs takes the first result in this order: conflict, same rule, same codes/different "
                   "details, different details. Total = 'Codes at both' in table 2.",
                   ["Client1", "Client2"] + RESULTS + ["Total codes"], rows))
    # distinct codes by result (one status per code) and rows by result
    for title, data in (("policy codes (CPT_Comparison)", explicit), ("mapped codes (CPT_Comparison_Mapped)", mapped)):
        rows3, rows4 = [], []
        for a, b in PAIRS:
            sub = [r for r in data if (r["Client1"], r["Client2"]) == (names[a], names[b])]
            per_code: Dict[str, Set[str]] = defaultdict(set)
            for r in sub:
                per_code[r["CPT_Code"]].add(r["Comparison_Result"])
            st = Counter(code_status(v) for v in per_code.values())
            rows3.append([names[a], names[b]] + [st.get(x, 0) for x in RESULTS] + [len(per_code)])
            rc = Counter(r["Comparison_Result"] for r in sub)
            rows4.append([names[a], names[b]] + [rc.get(x, 0) for x in RESULTS] + [len(sub)])
        tables.append((f"Distinct CPT codes by result - {title}  (a code with several rule pairs takes the first "
                       f"result in this order: conflict, same rule, same codes/different details, different details)",
                       ["Client1", "Client2"] + RESULTS + ["Total codes"], rows3))
        tables.append((f"Rows (CPT code x rule pair) by result - {title}",
                       ["Client1", "Client2"] + RESULTS + ["Total rows"], rows4))
    # 5. gaps
    rows = []
    for a, b in PAIRS:
        sub = [g for g in gaps if (g["Client1"], g["Client2"]) == (names[a], names[b])]
        for who in (names[a], names[b]):
            s_ = [g for g in sub if g["Has_Rule"] == who]
            rows.append([names[a], names[b], who, len({g["CPT_Code"] for g in s_}), len({g["Rule_ID"] for g in s_}),
                         sum(g["Gap_Severity"] == "HIGH" for g in s_), sum(g["Gap_Severity"] == "MEDIUM" for g in s_),
                         len(s_)])
    tables.append(("CPT codes only one client has a rule for (CPT_Only_One_Client)",
                   ["Client1", "Client2", "Has rule", "Distinct codes", "Rules involved", "HIGH rows", "MEDIUM rows",
                    "Total rows"], rows))
    return [(f"{i}. {t}", h, r) for i, (t, h, r) in enumerate(tables, 1)]


# ------------------------------------------------------------------------------
# output
# ------------------------------------------------------------------------------
def write_sheet(wb: Workbook, title: str, columns: Sequence[Tuple[str, int]], rows: Sequence[Dict],
                fill_col: Optional[str] = None) -> None:
    ws = wb.create_sheet(title)
    ws.append([c for c, _ in columns])
    for i, (c, w) in enumerate(columns, 1):
        cell = ws.cell(1, i)
        cell.font, cell.fill = HEADER_FONT, HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = w
    wrap, top = Alignment(wrap_text=True, vertical="top"), Alignment(vertical="top")
    fill_idx = next((i for i, (c, _) in enumerate(columns, 1) if c == fill_col), None)
    for r_i, row in enumerate(rows, 2):
        for c_i, (c, _) in enumerate(columns, 1):
            v = row.get(c, "")
            cell = ws.cell(r_i, c_i, v if v != "" else None)
            cell.alignment = wrap if c in WRAP_COLS else top
        if fill_idx and row.get(fill_col) in RESULT_FILL:
            ws.cell(r_i, fill_idx).fill = PatternFill("solid", fgColor=RESULT_FILL[row[fill_col]])
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(1, len(rows) + 1)}"


def write_summary(wb: Workbook, tables: List[Tuple[str, List[str], List[List]]]) -> None:
    ws = wb.create_sheet("Summary", 0)
    for i, w in enumerate([18, 18, 20, 20, 24, 22, 20, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = 1
    for title, header, rows in tables:
        ws.cell(r, 1, title).font = Font(bold=True, size=12, color="1F4E78")
        r += 1
        for i, h in enumerate(header, 1):
            c = ws.cell(r, i, h)
            c.font, c.fill = HEADER_FONT, HEADER_FILL
            c.alignment = Alignment(wrap_text=True, vertical="center")
            if h in RESULT_FILL:
                c.fill = PatternFill("solid", fgColor=RESULT_FILL[h])
                c.font = Font(bold=True)
        ws.row_dimensions[r].height = 45
        r += 1
        for row in rows:
            for i, v in enumerate(row, 1):
                ws.cell(r, i, v)
            r += 1
        r += 1


def write_read_me(wb: Workbook, names: Dict[str, str]) -> None:
    ws = wb.create_sheet("Read_Me")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 110
    pairs = ", ".join(f"{names[a]} vs {names[b]}" for a, b in PAIRS)
    entries = [
        ("Payer billing-rule comparison by CPT code", None),
        ("Pairs", f"Each pair is compared once as Client1 vs Client2: {pairs}. Filter Client1 and Client2 to pick a pair."),
        ("Grain", "One row per CPT code. Nothing is clubbed: a rule pair that shares 10 codes gives 10 rows, so you can "
                  "filter on any single CPT_Code."),
        ("Summary", "CPT-code counts per pair: how many codes both clients have rules for, how many only one has, and "
                    "the comparison result per code and per row."),
        ("CPT_Comparison", "CPT code x Client1 rule x Client2 rule, where both rules list the code. Codes written in the "
                           "policies (rule text, tables, attachments)."),
        ("CPT_Comparison_Mapped", "Same columns where at least one of the two rules only has codes mapped at policy level "
                                  "(the rule text names no code). Indicative - check the rule text."),
        ("CPT_Only_One_Client", "CPT code x rule where only one client of the pair has a rule listing the code."),
        ("Rules_Without_Codes", "Rules that list no code, once per payer. They cannot be compared by CPT code."),
        ("Completeness", "Every (rule, CPT code) of every pair is on a comparison sheet or on CPT_Only_One_Client, and "
                         "every rule without codes is on Rules_Without_Codes. The script stops if anything is missing, "
                         "repeated or mirrored."),
        ("", None),
        ("Comparison_Result", None),
        ("POSSIBLE_CONFLICT", "One payer pays and the other denies the code (from audit action and rule wording). Review first."),
        ("SAME_RULE", "Both rules list exactly the same codes and no rule detail differs."),
        ("SAME_CODES_DIFFERENT_RULE_DETAILS", "Same codes, but category / audit action / edit checks / ICD / percentages / "
                                              "modifiers differ."),
        ("DIFFERENT_DETAILS", "The two rules share this code but their code lists only partly overlap; other details may differ."),
        ("", None),
        ("Rule-pair columns", None),
        ("Code_Match_Type", "How the two rules' full code lists relate: SAME_CODES, CLIENT1_CODES_WITHIN_CLIENT2, "
                            "CLIENT2_CODES_WITHIN_CLIENT1, PARTIAL_OVERLAP."),
        ("Rules_Shared_Code_Count", "How many codes the two rules have in common in total (this row shows one of them)."),
        ("Code_Overlap_Pct", "Shared codes / all codes of both rules x 100."),
        ("Differences", "One line per difference: Outcome, Category, Audit action, Line of business, Codes, Conditions "
                        "(ICD-10), Edit checks, Percentages, Required / Prohibited modifiers."),
        ("Gap_Severity", "HIGH: the only rule on the code denies / limits payment. MEDIUM: it pays, adjusts or flags."),
    ]
    for i, (k, v) in enumerate(entries, 1):
        ws.cell(i, 1, k or None)
        if v is None:
            ws.cell(i, 1).font = Font(bold=True, size=12 if i > 1 else 14, color="1F4E78")
        else:
            ws.cell(i, 1).font = Font(bold=True)
            ws.cell(i, 2, v).alignment = Alignment(wrap_text=True, vertical="top")


def main() -> None:
    ap = argparse.ArgumentParser(description="CPT-code-level payer rule comparison")
    ap.add_argument("--dir", help="folder holding billing_rule_<payer>.xlsx (default: next to this script)")
    for k in PAYER_KEYS:
        ap.add_argument(f"--{k.lower()}", help=f"path to the {k} workbook")
    ap.add_argument("--out", default="comparison_report.xlsx")
    args = ap.parse_args()

    inputs = find_inputs(args)
    rules: Dict[str, List[Dict]] = {}
    for k, path in inputs.items():
        rules[k] = load_rules(path, k)
        print(f"{k:8s} {len(rules[k]):4d} rules   {path}")
    names = {k: (rules[k][0]["payer"] if rules[k] else k) for k in PAYER_KEYS}

    comp, gaps = [], []
    for i, (a, b) in enumerate(PAIRS):
        rows, g = compare_pair([r for r in rules[a] if r["code_set"]], [r for r in rules[b] if r["code_set"]])
        for x in rows + g:
            x["_key"] = (i,) + x["_key"]
        comp += rows
        gaps += g
        print(f"  {names[a]} vs {names[b]}: {len(rows)} code comparison rows, {len(g)} only-one-client rows")
    no_code = [row for k in PAYER_KEYS for row in no_code_rows(rules[k])]
    integrity_check(rules, names, comp, gaps, no_code)

    comp.sort(key=lambda r: r["_key"])
    gaps.sort(key=lambda r: r["_key"])
    explicit = [r for r in comp if not r["_mapped"]]
    mapped = [r for r in comp if r["_mapped"]]

    wb = Workbook()
    wb.remove(wb.active)
    write_summary(wb, summary_tables(names, rules, explicit, mapped, gaps))
    write_sheet(wb, "CPT_Comparison", COMPARISON_COLUMNS, explicit, "Comparison_Result")
    write_sheet(wb, "CPT_Comparison_Mapped", COMPARISON_COLUMNS, mapped, "Comparison_Result")
    write_sheet(wb, "CPT_Only_One_Client", ONLY_ONE_COLUMNS, gaps)
    write_sheet(wb, "Rules_Without_Codes", NO_CODE_COLUMNS, no_code)
    write_read_me(wb, names)
    out = Path(args.out)
    wb.save(out)
    print(f"Rows: CPT_Comparison {len(explicit)}, CPT_Comparison_Mapped {len(mapped)}, CPT_Only_One_Client {len(gaps)}, "
          f"Rules_Without_Codes {len(no_code)}")
    print(f"Integrity check passed (nothing missing, repeated or mirrored). Written: {out.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
