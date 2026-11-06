"""
reporter.py — Generate the multi-sheet Excel comparison report and
a Markdown executive summary.

Output: comparison_report/
    comparison_report.xlsx   — Full structured workbook (8 sheets)
    executive_summary.md     — High-level markdown report

Workbook sheets
---------------
1  Executive Summary      — statistics, tier distributions, top conflicts
2  Rule Matches           — all matched pairs with composite scores
3  Comparison Findings    — classified findings (EQUIVALENT, DIFFERING, CONFLICTING…)
4  Conflicting Rules      — subset: direct contradictions only
5  Differing Rules        — subset: same topic, different requirements
6  Coverage Gaps          — rules present in one payer only
7  Code Overlap Matrix    — which CPT codes appear in which payers
8  Policy Topic Coverage  — which policy topics are addressed by which payers
"""

from __future__ import annotations
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import List

import pandas as pd
import openpyxl
from openpyxl.styles import (PatternFill, Font, Alignment, Border, Side,
                              GradientFill)
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

from ingest import load_all
from matcher import match_rules, matches_to_df
from comparator import compare_all, findings_to_df
from gap_finder import find_gaps

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
OUT_DIR = Path(__file__).resolve().parent / "comparison_report"
OUT_DIR.mkdir(exist_ok=True)

XLSX_PATH = OUT_DIR / "comparison_report.xlsx"
MD_PATH   = OUT_DIR / "executive_summary.md"

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
_CLR = {
    "header_dark":   "1F3864",   # dark navy
    "header_mid":    "2F5496",   # mid blue
    "header_light":  "BDD7EE",   # pale blue
    "EQUIVALENT":    "C6EFCE",   # green
    "DIFFERING":     "FFEB9C",   # yellow
    "CONFLICTING":   "FFC7CE",   # red
    "ONE_SIDED":     "EDEDED",   # grey
    "NOT_ASSESSABLE":"F2F2F2",   # light grey
    "HIGH":          "FFC7CE",
    "MEDIUM":        "FFEB9C",
    "LOW":           "EBF0FF",
    "STRONG":        "C6EFCE",
    "POSSIBLE":      "FFEB9C",
    "WEAK":          "FFC7CE",
    "row_alt":       "F5F8FF",
    "white":         "FFFFFF",
}

_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _fill(hex_colour: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_colour)


def _hdr_font(dark: bool = True) -> Font:
    return Font(bold=True, color="FFFFFF" if dark else "1F3864", size=11)


def _apply_header(ws, row: int, columns: list, dark: bool = True):
    clr = _CLR["header_dark"] if dark else _CLR["header_mid"]
    for col_idx, label in enumerate(columns, 1):
        cell = ws.cell(row=row, column=col_idx, value=label)
        cell.fill   = _fill(clr)
        cell.font   = _hdr_font(dark)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER


def _auto_width(ws, min_w: int = 10, max_w: int = 60):
    for col in ws.columns:
        length = max(
            (len(str(c.value)) if c.value is not None else 0)
            for c in col
        )
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(length + 2, min_w), max_w)


def _write_df(ws, df: pd.DataFrame, start_row: int = 2, dark_header: bool = True,
              colour_col: str | None = None, colour_map: dict | None = None):
    """Write a DataFrame into a worksheet, optionally colouring rows by a column value."""
    headers = list(df.columns)
    _apply_header(ws, start_row - 1, headers, dark=dark_header)
    ws.row_dimensions[start_row - 1].height = 24

    for ridx, (_, row) in enumerate(df.iterrows()):
        actual_row = start_row + ridx
        for cidx, col in enumerate(headers, 1):
            val = row[col]
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            elif pd.isna(val):
                val = ""
            cell = ws.cell(row=actual_row, column=cidx, value=val)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = _BORDER

            # Row colouring
            bg = _CLR["white"] if ridx % 2 == 0 else _CLR["row_alt"]
            if colour_col and colour_map and col == colour_col:
                bg = colour_map.get(str(val), bg)
            if colour_col and colour_map:
                cell.fill = _fill(colour_map.get(str(row.get(colour_col, "")), bg))
            else:
                cell.fill = _fill(bg)

        ws.row_dimensions[actual_row].height = 36

    ws.freeze_panes = ws.cell(row=start_row, column=1)
    ws.auto_filter.ref = ws.dimensions


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------

def _sheet_summary(wb, stats: dict):
    ws = wb.create_sheet("Executive Summary", 0)
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 20

    title_font  = Font(bold=True, size=16, color="1F3864")
    sub_font    = Font(bold=True, size=12, color="2F5496")
    normal_font = Font(size=11)

    ws["A1"] = "Payer Billing Rule Comparison Report"
    ws["A1"].font = title_font
    ws["A2"] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A2"].font = Font(italic=True, size=10, color="808080")
    ws.row_dimensions[1].height = 30

    r = 4
    sections = [
        ("Rule Inventory", [
            ("Total rules loaded",          stats["total_rules"]),
            ("UHC rules",                   stats["uhc_rules"]),
            ("Aetna rules",                 stats["aetna_rules"]),
            ("Humana rules",                stats["humana_rules"]),
        ]),
        ("Match Analysis", [
            ("Total candidate pairs",       stats["total_matches"]),
            ("STRONG matches (≥ 0.70)",     stats["strong"]),
            ("POSSIBLE matches (≥ 0.50)",   stats["possible"]),
            ("WEAK matches (≥ 0.40)",       stats["weak"]),
        ]),
        ("Comparison Findings", [
            ("EQUIVALENT rules",            stats["EQUIVALENT"]),
            ("DIFFERING rules",             stats["DIFFERING"]),
            ("CONFLICTING rules",           stats["CONFLICTING"]),
            ("NOT_ASSESSABLE",              stats["NOT_ASSESSABLE"]),
        ]),
        ("Coverage Gaps", [
            ("Total unmatched rules",       stats["total_gaps"]),
            ("HIGH severity gaps",          stats["gap_HIGH"]),
            ("MEDIUM severity gaps",        stats["gap_MEDIUM"]),
            ("LOW severity gaps",           stats["gap_LOW"]),
        ]),
    ]

    for section_name, items in sections:
        ws.cell(row=r, column=1, value=section_name).font = sub_font
        ws.cell(row=r, column=1).fill = _fill(_CLR["header_light"])
        ws.cell(row=r, column=2).fill = _fill(_CLR["header_light"])
        r += 1
        for label, val in items:
            c1 = ws.cell(row=r, column=1, value=label)
            c2 = ws.cell(row=r, column=2, value=val)
            c1.font = normal_font
            c2.font = Font(bold=True, size=11)
            c2.alignment = Alignment(horizontal="center")
            bg = _CLR["white"] if r % 2 == 0 else _CLR["row_alt"]
            c1.fill = _fill(bg)
            c2.fill = _fill(bg)
            r += 1
        r += 1

    # Key takeaways section
    ws.cell(row=r, column=1, value="Key Takeaways").font = sub_font
    ws.cell(row=r, column=1).fill = _fill(_CLR["header_light"])
    r += 1
    takeaways = [
        f"• {stats['CONFLICTING']} directly conflicting rules detected — claims may be treated oppositely across payers.",
        f"• {stats['gap_HIGH']} high-severity coverage gaps (denial/frequency rules present in only one payer).",
        f"• {stats['DIFFERING']} rule pairs cover the same clinical area but impose different requirements.",
        f"• {stats['EQUIVALENT']} rule pairs are functionally equivalent across payers.",
        "• Review CONFLICTING sheet first; then HIGH-severity gaps for immediate risk remediation.",
    ]
    for t in takeaways:
        ws.cell(row=r, column=1, value=t).font = Font(size=11)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.row_dimensions[r].height = 18
        r += 1


def _sheet_matches(wb, df_matches: pd.DataFrame):
    ws = wb.create_sheet("Rule Matches")
    if df_matches.empty:
        ws["A1"] = "No matches found."
        return
    _write_df(ws, df_matches, start_row=2,
              colour_col="match_tier",
              colour_map={k: _CLR[k] for k in ["STRONG", "POSSIBLE", "WEAK"]})


def _sheet_findings(wb, df_findings: pd.DataFrame, sheet_name: str, filter_type: str | None = None):
    ws = wb.create_sheet(sheet_name)
    df = df_findings if filter_type is None else df_findings[df_findings["finding_type"] == filter_type].copy()
    if df.empty:
        ws["A1"] = f"No {filter_type or 'findings'} found."
        return
    _write_df(ws, df.reset_index(drop=True), start_row=2,
              colour_col="finding_type",
              colour_map={k: _CLR[k] for k in ["EQUIVALENT", "DIFFERING", "CONFLICTING",
                                                "ONE_SIDED", "NOT_ASSESSABLE"]})


def _sheet_gaps(wb, df_gaps: pd.DataFrame):
    ws = wb.create_sheet("Coverage Gaps")
    if df_gaps.empty:
        ws["A1"] = "No coverage gaps detected."
        return
    _write_df(ws, df_gaps, start_row=2,
              colour_col="gap_severity",
              colour_map={k: _CLR[k] for k in ["HIGH", "MEDIUM", "LOW"]})


def _sheet_code_matrix(wb, df_all: pd.DataFrame):
    """Which CPT codes appear in which payers, and how many rules reference each."""
    ws = wb.create_sheet("Code Overlap Matrix")

    payers = sorted(df_all["payer"].unique())
    code_data = {}

    for _, row in df_all.iterrows():
        for code in (row["cpt_codes"] if isinstance(row["cpt_codes"], list) else []):
            if code not in code_data:
                code_data[code] = {p: 0 for p in payers}
            code_data[code][row["payer"]] += 1

    if not code_data:
        ws["A1"] = "No code data."
        return

    headers = ["CPT/HCPCS Code"] + payers + ["Total Payers", "Total Rules"]
    _apply_header(ws, 1, headers)

    rows_out = []
    for code, pcounts in code_data.items():
        total_payers = sum(1 for v in pcounts.values() if v > 0)
        total_rules  = sum(pcounts.values())
        rows_out.append([code] + [pcounts[p] for p in payers] + [total_payers, total_rules])

    # Sort: most payers first, then most rules
    rows_out.sort(key=lambda x: (-x[-2], -x[-1]))

    for ridx, row in enumerate(rows_out):
        actual_row = ridx + 2
        bg = _CLR["white"] if ridx % 2 == 0 else _CLR["row_alt"]
        for cidx, val in enumerate(row, 1):
            cell = ws.cell(row=actual_row, column=cidx, value=val)
            cell.fill   = _fill(bg)
            cell.border = _BORDER
            cell.alignment = Alignment(horizontal="center")
        # Highlight codes present in all payers
        if row[-2] == len(payers):
            for cidx in range(1, len(headers) + 1):
                ws.cell(row=actual_row, column=cidx).fill = _fill(_CLR["EQUIVALENT"])

    _auto_width(ws)
    ws.freeze_panes = ws["A2"]


def _sheet_policy_coverage(wb, df_all: pd.DataFrame):
    """Policy topic × payer coverage matrix."""
    ws = wb.create_sheet("Policy Topic Coverage")

    payers = sorted(df_all["payer"].unique())
    # Normalise policy titles for grouping
    df_all = df_all.copy()
    df_all["_title_key"] = df_all["policy_title"].str.lower().str.strip()
    pivot = df_all.groupby(["_title_key", "payer"])["rule_id"].count().unstack(fill_value=0)
    pivot = pivot.reset_index().rename(columns={"_title_key": "Policy Topic"})
    pivot["Total Payers"] = (pivot[payers] > 0).sum(axis=1)
    pivot = pivot.sort_values(["Total Payers"] + payers, ascending=[False] + [False] * len(payers))

    headers = ["Policy Topic"] + payers + ["Total Payers"]
    _apply_header(ws, 1, headers)

    for ridx, (_, row) in enumerate(pivot.iterrows()):
        actual_row = ridx + 2
        bg = _CLR["white"] if ridx % 2 == 0 else _CLR["row_alt"]
        vals = [row["Policy Topic"]] + [int(row.get(p, 0)) for p in payers] + [int(row["Total Payers"])]
        for cidx, val in enumerate(vals, 1):
            cell = ws.cell(row=actual_row, column=cidx, value=val)
            cell.fill   = _fill(bg)
            cell.border = _BORDER
            cell.alignment = Alignment(horizontal="center" if cidx > 1 else "left")
        if int(row["Total Payers"]) == len(payers):
            for cidx in range(1, len(headers) + 1):
                ws.cell(row=actual_row, column=cidx).fill = _fill(_CLR["EQUIVALENT"])

    _auto_width(ws)
    ws.freeze_panes = ws["A2"]


# ---------------------------------------------------------------------------
# Markdown executive summary
# ---------------------------------------------------------------------------

def _write_md(stats: dict, df_findings: pd.DataFrame, df_gaps: pd.DataFrame):
    lines = [
        "# Payer Billing Rule Comparison — Executive Summary",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        "## 1. Rule Inventory",
        "",
        f"| Payer | Rules |",
        f"|-------|-------|",
        f"| UHC | {stats['uhc_rules']} |",
        f"| Aetna | {stats['aetna_rules']} |",
        f"| Humana | {stats['humana_rules']} |",
        f"| **Total** | **{stats['total_rules']}** |",
        "",
        "---",
        "",
        "## 2. Match Analysis",
        "",
        f"| Tier | Pairs |",
        f"|------|-------|",
        f"| STRONG (composite ≥ 0.70) | {stats['strong']} |",
        f"| POSSIBLE (composite ≥ 0.50) | {stats['possible']} |",
        f"| WEAK (composite ≥ 0.40) | {stats['weak']} |",
        f"| **Total candidate pairs** | **{stats['total_matches']}** |",
        "",
        "---",
        "",
        "## 3. Comparison Findings",
        "",
        f"| Finding Type | Count |",
        f"|--------------|-------|",
        f"| ✅ EQUIVALENT | {stats['EQUIVALENT']} |",
        f"| ⚠️ DIFFERING | {stats['DIFFERING']} |",
        f"| 🚨 CONFLICTING | {stats['CONFLICTING']} |",
        f"| ❓ NOT_ASSESSABLE | {stats['NOT_ASSESSABLE']} |",
        "",
    ]

    if not df_findings.empty:
        conflicts = df_findings[df_findings["finding_type"] == "CONFLICTING"]
        if not conflicts.empty:
            lines += [
                "### Top Conflicting Rule Pairs",
                "",
                "| Rule A | Rule B | Detail |",
                "|--------|--------|--------|",
            ]
            for _, r in conflicts.head(10).iterrows():
                lines.append(
                    f"| [{r['payer_a']}] {str(r['statement_a'])[:80]}… "
                    f"| [{r['payer_b']}] {str(r['statement_b'])[:80]}… "
                    f"| {str(r['finding_detail'])[:120]} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "## 4. Coverage Gaps",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
        f"| 🔴 HIGH | {stats['gap_HIGH']} |",
        f"| 🟡 MEDIUM | {stats['gap_MEDIUM']} |",
        f"| 🟢 LOW | {stats['gap_LOW']} |",
        f"| **Total unmatched rules** | **{stats['total_gaps']}** |",
        "",
    ]

    if not df_gaps.empty:
        high_gaps = df_gaps[df_gaps["gap_severity"] == "HIGH"].head(10)
        if not high_gaps.empty:
            lines += [
                "### Top HIGH-Severity Gaps",
                "",
                "| Payer | Policy | Category | Missing In | Rule (excerpt) |",
                "|-------|--------|----------|-----------|----------------|",
            ]
            for _, r in high_gaps.iterrows():
                lines.append(
                    f"| {r['payer']} | {r['policy_title'][:40]} | {r['rule_category']} "
                    f"| {r['missing_in']} | {str(r['rule_statement'])[:100]}… |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "## 5. Next Steps",
        "",
        "1. **Review CONFLICTING rules first** — these represent the highest risk of inconsistent claim adjudication.",
        "2. **Address HIGH-severity coverage gaps** — missing denial or frequency rules may cause over-payment.",
        "3. **Harmonise DIFFERING rules** — align frequency limits, modifier requirements, and code sets.",
        "4. **Validate EQUIVALENT rules** — confirm they truly have the same clinical intent before treating as aligned.",
        "",
        f"Full details in: `comparison_report.xlsx`",
    ]

    MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Executive summary written → {MD_PATH}")


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------

def run():
    print("Loading rules from all payers …")
    df_all = load_all()
    print(f"  {len(df_all)} rules loaded ({df_all.groupby('payer')['rule_id'].count().to_dict()})")

    print("Finding candidate matches …")
    matches = match_rules(df_all)
    print(f"  {len(matches)} candidate pairs found")

    print("Classifying comparison findings …")
    findings  = compare_all(matches, df_all)
    df_matches  = matches_to_df(matches)
    df_findings = findings_to_df(findings)

    print("Identifying coverage gaps …")
    df_gaps = find_gaps(df_all, matches)
    print(f"  {len(df_gaps)} unmatched rules (gaps)")

    # Compute statistics
    ftypes = Counter(f.finding_type for f in findings)
    tiers  = Counter(m.match_tier for m in matches)
    gsevs  = Counter(df_gaps["gap_severity"].tolist()) if not df_gaps.empty else {}

    stats = {
        "total_rules":   len(df_all),
        "uhc_rules":     int((df_all["payer"] == "UHC").sum()),
        "aetna_rules":   int((df_all["payer"] == "Aetna").sum()),
        "humana_rules":  int((df_all["payer"] == "Humana").sum()),
        "total_matches": len(matches),
        "strong":        tiers.get("STRONG", 0),
        "possible":      tiers.get("POSSIBLE", 0),
        "weak":          tiers.get("WEAK", 0),
        "EQUIVALENT":    ftypes.get("EQUIVALENT", 0),
        "DIFFERING":     ftypes.get("DIFFERING", 0),
        "CONFLICTING":   ftypes.get("CONFLICTING", 0),
        "NOT_ASSESSABLE":ftypes.get("NOT_ASSESSABLE", 0),
        "total_gaps":    len(df_gaps),
        "gap_HIGH":      gsevs.get("HIGH", 0),
        "gap_MEDIUM":    gsevs.get("MEDIUM", 0),
        "gap_LOW":       gsevs.get("LOW", 0),
    }

    print("Building Excel workbook …")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)   # remove default empty sheet

    _sheet_summary(wb, stats)
    _sheet_matches(wb, df_matches)
    _sheet_findings(wb, df_findings, "Comparison Findings")
    _sheet_findings(wb, df_findings, "Conflicting Rules",  filter_type="CONFLICTING")
    _sheet_findings(wb, df_findings, "Differing Rules",    filter_type="DIFFERING")
    _sheet_gaps(wb, df_gaps)
    _sheet_code_matrix(wb, df_all)
    _sheet_policy_coverage(wb, df_all)

    # Auto-widen all sheets
    for ws in wb.worksheets:
        _auto_width(ws)

    wb.save(XLSX_PATH)
    print(f"  Workbook saved → {XLSX_PATH}")

    _write_md(stats, df_findings, df_gaps)

    print("\n✅ Done. Summary:")
    for k, v in stats.items():
        print(f"   {k:25s}: {v}")
    return stats


if __name__ == "__main__":
    run()

