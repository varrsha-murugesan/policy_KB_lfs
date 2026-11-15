#!/usr/bin/env python3
"""
compare_rules.py - cross-payer billing rule comparison.

Reads any number of billing_rules_<client>.xlsx workbooks (the output of the
generate_billing_rules.py scripts) and writes ONE report with two sheets.

Matching is by CPT/HCPCS code only: two rules from different payers are a match
when they are written for at least one common code. Rule wording, policy titles
and categories are NOT used to decide a match; they are only shown side by side.
(Optional: rules split per condition must also share an ICD-10 category, see
MATCH_ON_ICD below.)

  Rule_Comparison  - one row per matched rule pair. Client1 / Client2 hold the
                     payer names; every pair is written in both directions, so
                     filtering Client1 = X and Client2 = Y always shows the
                     X-vs-Y comparison, however many payers are loaded.
  Unmatched_Rules  - every rule that has no counterpart in another payer
                     (one row per rule and per payer it is missing from).

Usage
  python compare_rules.py                               # finds billing_rules_*.xlsx under the parent folder
  python compare_rules.py --inputs ../Uhc ../Humana     # folders and/or files
  python compare_rules.py --inputs a.xlsx b.xlsx --out comparison_report.xlsx

Only pandas and openpyxl are needed.
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ----------------------------------------------------------------------------
# Settings (tune here)
# ----------------------------------------------------------------------------
MATCH_ON_ICD = True   # when BOTH rules list ICD-10 codes, also require a common ICD-10 category
                      # (stops "flu test for abdominal pain" matching "flu test for dehydration").
                      # Set False for a pure CPT-only match.
MAX_CELL = 32000                 # Excel cell limit is 32,767 characters
MAX_CODES_SHOWN = 400

HIGH_GAP_CATEGORIES = {"DENIAL_PROHIBITION", "FREQUENCY_LIMIT", "TIME_SPAN_LIMIT",
                       "ONCE_IN_A_LIFETIME", "BUNDLING_INCLUSION", "ADD_ON_PRIMARY"}


CODE_FULL_RE = re.compile(r"\d{4}[0-9A-Z]|[A-V]\d{4}")
CODE_RE = re.compile(r"\b(?:\d{4}[0-9A-Z]|[A-V]\d{4})\b")
ICD_CELL_RE = re.compile(r"\b[A-TV-Z]\d[0-9A-Z]{1,5}\b")   # ICD-10 codes as stored (dot optional)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I)


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def find_inputs(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += glob.glob(os.path.join(p, "**", "billing_rules_*.xlsx"), recursive=True)
        elif any(ch in p for ch in "*?["):
            files += glob.glob(p, recursive=True)
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"  ! not found: {p}")
    files = [f for f in files if not os.path.basename(f).startswith("~$")]
    return sorted(set(os.path.abspath(f) for f in files))


def client_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^billing_rules_?", "", stem, flags=re.I)
    return stem.replace("_", " ").title() or stem


def s(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def expand_code_text(text):
    """Codes from a Target_Codes cell such as '84403, 90963-90966, G0303'."""
    out = set()
    for part in re.split(r"[,;\n]", text):
        part = part.strip()
        m = re.fullmatch(r"([A-Z]?)(\d{4,5})([A-Z]?)\s*[-–]\s*([A-Z]?)(\d{4,5})([A-Z]?)", part)
        if m and m.group(1) == m.group(4) and m.group(3) == m.group(6):
            a, b = int(m.group(2)), int(m.group(5))
            if 0 <= b - a <= 2000:
                w = len(m.group(2))
                out |= {f"{m.group(1)}{str(i).zfill(w)}{m.group(3)}" for i in range(a, b + 1)}
                continue
        out |= set(CODE_RE.findall(part))
    return out


def load_workbook(path):
    xl = pd.ExcelFile(path)
    sheet = "Rule_Detail" if "Rule_Detail" in xl.sheet_names else "Rules"
    df = xl.parse(sheet, dtype=str).fillna("")
    if "Rule_ID" not in df.columns:
        raise ValueError(f"{path}: sheet {sheet} has no Rule_ID column")
    payer = ""
    if "Payer" in df.columns and (df["Payer"] != "").any():
        payer = df.loc[df["Payer"] != "", "Payer"].mode().iloc[0]
    client = payer or client_from_filename(path)

    # full code sets (attachments included) come from Rule_Code_Map when present
    codes_by_rule = defaultdict(set)
    if "Rule_Code_Map" in xl.sheet_names:
        cm = xl.parse("Rule_Code_Map", dtype=str, usecols=lambda c: c in ("Rule_ID", "Code", "Related_Code"))
        cm = cm.fillna("")
        for rid, code, rel in cm.itertuples(index=False):
            code = code.strip().upper()
            if CODE_FULL_RE.fullmatch(code):          # CPT / HCPCS only (skips modifiers etc.)
                codes_by_rule[rid].add(code)
            # Related_Code holds the partner code (the primary of an add-on, the component of a
            # comprehensive code); it is context, not a code the rule is written for

    rules = []
    for r in df.to_dict("records"):
        rid = s(r.get("Rule_ID"))
        if not rid:
            continue
        codes = codes_by_rule.get(rid) or expand_code_text(s(r.get("Target_Codes")))
        rules.append({
            "client": client,
            "file": os.path.basename(path),
            "rule_id": rid,
            "policy_title": s(r.get("Policy_Title")),
            "policy_number": s(r.get("Policy_Number")),
            "lob": s(r.get("Line_of_Business")),
            "category": s(r.get("Rule_Category")) or "POLICY_REQUIREMENT",
            "audit_action": s(r.get("Audit_Action")),
            "rule_text": s(r.get("Policy_Rule_Text")),
            "target_codes": s(r.get("Target_Codes")),
            "codes": frozenset(codes),
            "modifiers_required": norm_list(s(r.get("Modifiers_Required"))),
            "modifiers_prohibited": norm_list(s(r.get("Modifiers_Prohibited"))),
            "icd": s(r.get("Possible_icd_codes")),
            "icd_set": frozenset(c[:3] for c in ICD_CELL_RE.findall(s(r.get("Possible_icd_codes")).upper().replace(".", ""))),
            "attachment": s(r.get("Attachment_Reference")),
            "source_pdf": s(r.get("Source_PDF")),
            "edit_checks": edit_checks(s(r.get("SQL_Query"))),
            "negative": is_negative(s(r.get("Policy_Rule_Text"))),
            "percents": frozenset(PCT_RE.findall(s(r.get("Policy_Rule_Text")))),
        })
    return client, rules


def norm_list(text):
    return frozenset(t.strip().upper() for t in re.split(r"[,;|/\s]+", text) if t.strip())


_NEG_RE = re.compile(r"\b(?:will not (?:consider )?reimburse|not (?:be )?reimbursable|is not reimbursable|"
                     r"not (?:be )?covered|is not covered|will not (?:be )?(?:paid|allowed)|not separately reimbursable|"
                     r"will be denied|are denied|is denied|not eligible for reimbursement)", re.I)
_POS_RE = re.compile(r"\b(?:will consider reimbursement|is reimbursable|are reimbursable|is covered|are covered|"
                     r"will (?:be )?(?:paid|allowed|reimbursed))\b", re.I)


def is_negative(text):
    return len(_NEG_RE.findall(text)) > len(_POS_RE.findall(text))


_SKIP_COMMENT = re.compile(r"^(?:NEEDS INPUT|ICD-10 from|TODO|lines carrying)", re.I)


def edit_checks(sql):
    """The violation checks a query tests, read from its inline '--' comments."""
    if not sql:
        return ()
    checks = []
    for i, line in enumerate(sql.splitlines()):
        if i < 3 and line.lstrip().startswith("--"):
            continue  # header lines (rule id, rule text, needs input)
        for m in re.finditer(r"--\s*(.+)$", line):
            c = m.group(1).strip()
            if c and not _SKIP_COMMENT.match(c):
                checks.append(c)
        for m in re.finditer(r"units_billed\)?\s*>\s*(\d+)", line):
            checks.append(f"units > {m.group(1)}")
    return tuple(dict.fromkeys(checks))


# ----------------------------------------------------------------------------
# Code-based matching
# ----------------------------------------------------------------------------
def code_matches(rules_a, rules_b):
    """{(i, j): shared_codes} for every rule pair with at least one common CPT/HCPCS code."""
    index = defaultdict(list)
    for j, r in enumerate(rules_b):
        for c in r["codes"]:
            index[c].append(j)
    pairs = {}
    for i, r in enumerate(rules_a):
        hits = defaultdict(set)
        for c in r["codes"]:
            for j in index.get(c, ()):
                hits[j].add(c)
        for j, shared in hits.items():
            pairs[(i, j)] = frozenset(shared)
    return pairs


def icd_compatible(a, b):
    if not MATCH_ON_ICD or not a["icd_set"] or not b["icd_set"]:
        return True
    return bool(a["icd_set"] & b["icd_set"])


def code_match_type(a, b):
    if a["codes"] == b["codes"]:
        return "SAME_CODES"
    if a["codes"] < b["codes"]:
        return "CLIENT1_CODES_WITHIN_CLIENT2"
    if b["codes"] < a["codes"]:
        return "CLIENT2_CODES_WITHIN_CLIENT1"
    return "PARTIAL_OVERLAP"


# ----------------------------------------------------------------------------
# Comparison of a matched pair
# ----------------------------------------------------------------------------
def disposition(action, negative=False):
    a = action.upper()
    if negative and a.startswith("PAY"):
        return "DENY"
    if a.startswith("DENY"):
        return "DENY"
    if a.startswith("PAY"):
        return "PAY"
    if a.startswith("ADJUST"):
        return "ADJUST"
    if a.startswith("BYPASS"):
        return "EXCEPTION"
    return "REVIEW"


# a denial that only covers "everything else" or a combination/quantity limit does not
# contradict another payer paying the code for a specific reason
_NOT_A_CONFLICT = re.compile(r"\b(?:all other|any other|not (?:described|listed|meeting|met)|otherwise|"
                             r"with another|in addition to|same (?:date|day)|more than|exceed|"
                             r"in combination|together with|separately)\b", re.I)


def compare(a, b, client_a, client_b):
    """Differences in the rule parameters for two rules that share codes."""
    diffs = []
    da, db = disposition(a["audit_action"], a["negative"]), disposition(b["audit_action"], b["negative"])
    outright = {"DENY_LINE", "PAY_IF_CRITERIA_MET"}
    conflict = ({da, db} == {"PAY", "DENY"}
                and a["audit_action"] in outright and b["audit_action"] in outright
                and not _NOT_A_CONFLICT.search((a if da == "DENY" else b)["rule_text"]))
    if conflict:
        diffs.append(f"Outcome: {client_a} {'pays' if da == 'PAY' else 'denies'}, "
                     f"{client_b} {'pays' if db == 'PAY' else 'denies'} the shared codes")
    if a["category"] != b["category"]:
        diffs.append(f"Category: {client_a} {a['category']} vs {client_b} {b['category']}")
    if a["audit_action"] != b["audit_action"]:
        diffs.append(f"Audit action: {client_a} {a['audit_action']} vs {client_b} {b['audit_action']}")
    if a["lob"] != b["lob"]:
        diffs.append(f"Line of business: {client_a} {a['lob'] or '-'} vs {client_b} {b['lob'] or '-'}")
    only_a, only_b = a["codes"] - b["codes"], b["codes"] - a["codes"]
    if only_a or only_b:
        diffs.append(f"Codes: {len(a['codes'] & b['codes'])} shared, {len(only_a)} only in {client_a}, "
                     f"{len(only_b)} only in {client_b}")
    xa, xb = unmatched_checks(a["edit_checks"], b["edit_checks"])
    if xa:
        diffs.append(f"Edit checks only in {client_a}: " + "; ".join(xa))
    if xb:
        diffs.append(f"Edit checks only in {client_b}: " + "; ".join(xb))
    icd_same = a["icd_set"] == b["icd_set"]
    if not icd_same:
        if a["icd_set"] and b["icd_set"]:
            diffs.append(f"Conditions (ICD-10 categories): {client_a} {fmt_set(a['icd_set'])} vs "
                         f"{client_b} {fmt_set(b['icd_set'])}")
        else:
            who = client_a if a["icd_set"] else client_b
            diffs.append(f"Conditions: only {who} lists ICD-10 codes")
    if a["modifiers_required"] != b["modifiers_required"]:
        diffs.append(f"Required modifiers: {client_a} {fmt_set(a['modifiers_required'])} vs "
                     f"{client_b} {fmt_set(b['modifiers_required'])}")
    if a["modifiers_prohibited"] != b["modifiers_prohibited"]:
        diffs.append(f"Prohibited modifiers: {client_a} {fmt_set(a['modifiers_prohibited'])} vs "
                     f"{client_b} {fmt_set(b['modifiers_prohibited'])}")
    if a["percents"] != b["percents"] and (a["percents"] or b["percents"]):
        diffs.append(f"Percentages: {client_a} {fmt_set(a['percents'], '%')} vs "
                     f"{client_b} {fmt_set(b['percents'], '%')}")

    if conflict:
        result = "POSSIBLE_CONFLICT"
    elif not diffs or (len(diffs) == 1 and diffs[0].startswith("Line of business")):
        result = "SAME_RULE"
    else:
        result = "SAME_CODES_DIFFERENT_RULE_DETAILS" if not (only_a or only_b) else "DIFFERENT_DETAILS"
    return result, diffs


_SPAN_RE = re.compile(r"within (\d+) (day|week|month|year)\(s\)")
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30.4, "year": 365}


def canon_check(c):
    """Comparable form of an edit check: time spans in days, so 3 months == 90 days."""
    m = _SPAN_RE.search(c)
    if m:
        return ("span", int(m.group(1)) * _UNIT_DAYS[m.group(2)])
    return ("text", re.sub(r"\s+", " ", c.lower()))


def unmatched_checks(ca, cb):
    """Edit checks of each rule that have no equivalent in the other (5% tolerance on spans)."""
    ka, kb = [canon_check(c) for c in ca], [canon_check(c) for c in cb]

    def same(x, y):
        if x[0] != y[0]:
            return False
        if x[0] == "span":
            return abs(x[1] - y[1]) <= 0.05 * max(x[1], y[1])
        return x[1] == y[1]

    only_a = [c for c, k in zip(ca, ka) if not any(same(k, o) for o in kb)]
    only_b = [c for c, k in zip(cb, kb) if not any(same(k, o) for o in ka)]
    return only_a, only_b


def fmt_set(x, suffix=""):
    return ", ".join(sorted(f"{v}{suffix}" for v in x)) if x else "none"


def code_key(c):
    m = re.match(r"([A-Z]?)(\d+)([A-Z]?)$", c)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (c, 0, "")


def compress_codes(codes):
    """Sorted codes with consecutive runs written as ranges, truncated for Excel."""
    if not codes:
        return ""
    items = sorted(codes, key=code_key)
    out, run = [], [items[0]]

    def flush():
        if len(run) >= 3:
            out.append(f"{run[0]}-{run[-1]}")
        else:
            out.extend(run)

    for c in items[1:]:
        p, q = code_key(run[-1]), code_key(c)
        if p[0] == q[0] and p[2] == q[2] and q[1] == p[1] + 1 and len(c) == len(run[-1]):
            run.append(c)
        else:
            flush()
            run = [c]
    flush()
    if len(out) > MAX_CODES_SHOWN:
        return ", ".join(out[:MAX_CODES_SHOWN]) + f", … (+{len(out) - MAX_CODES_SHOWN} more groups)"
    return ", ".join(out)


def gap_severity(r):
    if r["codes"] and (r["category"] in HIGH_GAP_CATEGORIES or r["audit_action"].startswith("DENY")):
        return "HIGH"
    if r["codes"]:
        return "MEDIUM"
    return "LOW"


def cap(text):
    text = s(text)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + " …(truncated)"


# ----------------------------------------------------------------------------
# Main comparison
# ----------------------------------------------------------------------------
def run(files, out_path):
    clients, source = {}, {}
    for f in files:
        client, rules = load_workbook(f)
        if client in clients:
            # the same payer found twice (e.g. an old copy): keep the newest workbook
            keep_new = os.path.getmtime(f) > os.path.getmtime(source[client])
            print(f"  ! {client} found twice; using the newer file "
                  f"{os.path.basename(f if keep_new else source[client])}")
            if not keep_new:
                continue
        clients[client], source[client] = rules, f
        print(f"  loaded {client:<20} {len(rules):>5} rules  ({f})")
    if len(clients) < 2:
        sys.exit("Need workbooks from at least two different payers.")

    names = list(clients)
    comp_rows = []
    # status[(client, rule_id)][other] = ("MATCHED" | "ICD_DIFFERENT" | "NO_SHARED_CODES", closest rule, shared)
    status = defaultdict(dict)
    for ca, cb in combinations(names, 2):
        A, B = clients[ca], clients[cb]
        for (i, j), shared in code_matches(A, B).items():
            a, b = A[i], B[j]
            ok = icd_compatible(a, b)
            for (c1, r1, c2, r2) in ((ca, a, cb, b), (cb, b, ca, a)):
                prev = status[(c1, r1["rule_id"])].get(c2)
                if ok:
                    if not prev or prev[0] != "MATCHED" or len(shared) > len(prev[2]):
                        status[(c1, r1["rule_id"])][c2] = ("MATCHED", r2, shared)
                elif not prev or (prev[0] == "ICD_DIFFERENT" and len(shared) > len(prev[2])):
                    status[(c1, r1["rule_id"])][c2] = ("ICD_DIFFERENT", r2, shared)
            if not ok:
                continue
            for c1, r1, c2, r2 in ((ca, a, cb, b), (cb, b, ca, a)):
                result, diffs = compare(r1, r2, c1, c2)
                union = r1["codes"] | r2["codes"]
                row = {
                    "Client1": c1, "Client2": c2,
                    "Code_Match_Type": code_match_type(r1, r2),
                    "Comparison_Result": result,
                    "Differences": cap("\n".join(diffs)) if diffs else "No differences found",
                    "Shared_Code_Count": len(shared),
                    "Shared_Codes": cap(compress_codes(shared)),
                    "Code_Overlap_Pct": round(100 * len(shared) / len(union), 1),
                    "Pct_Of_Client1_Codes_Shared": round(100 * len(shared) / len(r1["codes"]), 1),
                    "Pct_Of_Client2_Codes_Shared": round(100 * len(shared) / len(r2["codes"]), 1),
                    "Codes_Only_In_Client1": cap(compress_codes(r1["codes"] - r2["codes"])),
                    "Codes_Only_In_Client2": cap(compress_codes(r2["codes"] - r1["codes"])),
                }
                row.update(rule_cols("Client1", r1))
                row.update(rule_cols("Client2", r2))
                comp_rows.append(row)

    unmatched_rows = []
    for c, rs in clients.items():
        for r in rs:
            for other in names:
                if other == c:
                    continue
                st = status[(c, r["rule_id"])].get(other)
                if st and st[0] == "MATCHED":
                    continue
                if not r["codes"]:
                    reason = "Rule names no CPT/HCPCS codes (policy-wide rule), so it cannot be code-matched"
                elif st:
                    reason = (f"Codes also appear in {other} rules, but for different conditions "
                              f"(no common ICD-10 category)")
                else:
                    reason = f"None of the rule's codes appear in any {other} rule"
                cand, shared = (st[1], st[2]) if st else (None, frozenset())
                unmatched_rows.append({
                    "Client": c, "Not_Matched_In": other, "Unmatched_Reason": reason,
                    "Gap_Severity": gap_severity(r), "Rule_ID": r["rule_id"],
                    "Policy_Title": r["policy_title"], "Policy_Number": r["policy_number"],
                    "Line_of_Business": r["lob"], "Rule_Category": r["category"],
                    "Audit_Action": r["audit_action"], "Code_Count": len(r["codes"]),
                    "Target_Codes": cap(compress_codes(r["codes"]) or r["target_codes"]),
                    "Policy_Rule_Text": cap(r["rule_text"]), "Edit_Checks": cap("\n".join(r["edit_checks"])),
                    "Possible_icd_codes": cap(r["icd"]), "Source_PDF": r["source_pdf"],
                    "Same_Codes_Rule_In_Other_Payer": cand["rule_id"] if cand else "",
                    "Other_Rule_ICD_Codes": cap(cand["icd"]) if cand else "",
                    "Shared_Code_Count": len(shared),
                })

    comp_rows.sort(key=lambda r: (r["Client1"], r["Client2"], r["Client1_Policy_Title"],
                                  r["Client1_Rule_ID"], -r["Shared_Code_Count"], r["Client2_Rule_ID"]))
    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    unmatched_rows.sort(key=lambda r: (r["Client"], r["Not_Matched_In"], sev_order[r["Gap_Severity"]],
                                       r["Policy_Title"], r["Rule_ID"]))
    write_report(out_path, comp_rows, unmatched_rows)
    return clients, comp_rows, unmatched_rows


def rule_cols(prefix, r):
    return {
        f"{prefix}_Rule_ID": r["rule_id"],
        f"{prefix}_Policy_Title": r["policy_title"],
        f"{prefix}_Policy_Number": r["policy_number"],
        f"{prefix}_LOB": r["lob"],
        f"{prefix}_Category": r["category"],
        f"{prefix}_Audit_Action": r["audit_action"],
        f"{prefix}_Code_Count": len(r["codes"]),
        f"{prefix}_Rule_Text": cap(r["rule_text"]),
        f"{prefix}_Edit_Checks": cap("\n".join(r["edit_checks"])),
        f"{prefix}_ICD_Codes": cap(r["icd"]),
    }


COMP_COLUMNS = ["Client1", "Client2", "Shared_Code_Count", "Shared_Codes", "Code_Match_Type",
                "Comparison_Result", "Differences",
                "Client1_Rule_ID", "Client2_Rule_ID", "Client1_Policy_Title", "Client2_Policy_Title",
                "Client1_Category", "Client2_Category", "Client1_Audit_Action", "Client2_Audit_Action",
                "Client1_Rule_Text", "Client2_Rule_Text",
                "Codes_Only_In_Client1", "Codes_Only_In_Client2",
                "Code_Overlap_Pct", "Pct_Of_Client1_Codes_Shared", "Pct_Of_Client2_Codes_Shared",
                "Client1_Code_Count", "Client2_Code_Count",
                "Client1_Edit_Checks", "Client2_Edit_Checks", "Client1_ICD_Codes", "Client2_ICD_Codes",
                "Client1_LOB", "Client2_LOB", "Client1_Policy_Number", "Client2_Policy_Number"]
UNMATCHED_COLUMNS = ["Client", "Not_Matched_In", "Gap_Severity", "Unmatched_Reason", "Rule_ID", "Policy_Title",
                     "Policy_Number", "Line_of_Business", "Rule_Category", "Audit_Action", "Code_Count",
                     "Target_Codes", "Policy_Rule_Text", "Edit_Checks", "Possible_icd_codes", "Source_PDF",
                     "Same_Codes_Rule_In_Other_Payer", "Other_Rule_ICD_Codes", "Shared_Code_Count"]
WIDE = {"Differences": 55, "Client1_Rule_Text": 70, "Client2_Rule_Text": 70, "Policy_Rule_Text": 80,
        "Shared_Codes": 30, "Codes_Only_In_Client1": 30, "Codes_Only_In_Client2": 30, "Target_Codes": 30,
        "Client1_Edit_Checks": 32, "Client2_Edit_Checks": 32, "Edit_Checks": 32, "Unmatched_Reason": 40,
        "Client1_Policy_Title": 30, "Client2_Policy_Title": 30, "Policy_Title": 32,
        "Client1_ICD_Codes": 24, "Client2_ICD_Codes": 24, "Possible_icd_codes": 24, "Other_Rule_ICD_Codes": 24}
FILLS = {"SAME_RULE": "C6EFCE", "SAME_CODES_DIFFERENT_RULE_DETAILS": "FFEB9C", "DIFFERENT_DETAILS": "FCE4D6",
         "POSSIBLE_CONFLICT": "FFC7CE", "SAME_CODES": "C6EFCE",
         "HIGH": "FFC7CE", "MEDIUM": "FFEB9C", "LOW": "DDEBF7"}


def write_sheet(ws, columns, rows, colour_cols):
    header_fill = PatternFill("solid", fgColor="1F4E78")
    ws.append(columns)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = header_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    fills = {k: PatternFill("solid", fgColor=v) for k, v in FILLS.items()}
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")
    idx = {c: i for i, c in enumerate(columns)}
    for r in rows:
        ws.append([r.get(c, "") for c in columns])
        cells = ws[ws.max_row]
        for c in cells:
            c.alignment = wrap if columns[c.column - 1] in WIDE else top
        for col in colour_cols:
            cell = cells[idx[col]]
            f = fills.get(str(cell.value))
            if f:
                cell.fill = f
    for i, c in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = WIDE.get(c, max(12, min(26, len(c) + 2)))
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(ws.max_row, 2)}"
    ws.row_dimensions[1].height = 30


def write_report(path, comp_rows, unmatched_rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Rule_Comparison"
    write_sheet(ws, COMP_COLUMNS, comp_rows, ["Code_Match_Type", "Comparison_Result"])
    ws2 = wb.create_sheet("Unmatched_Rules")
    write_sheet(ws2, UNMATCHED_COLUMNS, unmatched_rows, ["Gap_Severity"])
    wb.save(path)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Compare billing rules across payers by CPT/HCPCS code.")
    ap.add_argument("--inputs", nargs="*", default=[os.path.dirname(here)],
                    help="billing_rules_<client>.xlsx files and/or folders to search (default: parent folder)")
    ap.add_argument("--out", default=os.path.join(here, "comparison_report.xlsx"))
    args = ap.parse_args()

    files = find_inputs(args.inputs)
    if not files:
        sys.exit("No billing_rules_*.xlsx files found. Pass them with --inputs.")
    print(f"Comparing {len(files)} workbook(s):")
    clients, comp, unmatched = run(files, args.out)

    print(f"\nRule_Comparison: {len(comp)} rows ({len(comp) // 2} code-matched rule pairs, written both ways)")
    for k, v in sorted(Counter(r["Comparison_Result"] for r in comp).items()):
        print(f"  {k:<34} {v // 2}")
    print(f"Unmatched_Rules: {len(unmatched)} rows")
    for c in clients:
        n = sum(1 for r in unmatched if r["Client"] == c)
        print(f"  {c:<20} {n}")
    print(f"\nReport: {args.out}")


if __name__ == "__main__":
    main()
