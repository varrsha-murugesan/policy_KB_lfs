#!/usr/bin/env python3
"""
generate_billing_rules.py
=========================
Humana deterministic billing-rule generator (Medicare Advantage only).

Input : extracted_rules.json
Output: billing_rules_humana.{xlsx,json,csv} + billing_rules_humana_code_map.csv

How rules are picked (see rule_core.py for the shared keyword lexicon):
  1. Blocks are stitched back into their sections. The PDF extractor splits a
     section at page breaks into 'paragraph' + 'bullet_point' blocks that start
     mid-sentence; those are re-joined so no rule is cut in half.
  2. Overview / Definitions / Medicaid-only sections and corpus boilerplate
     ("claims payments are subject to other plan requirements ...") are skipped.
  3. A section becomes a rule only if it contains a DIRECTIVE keyword. Short
     sections are kept whole (complete, verbatim). In long sections every
     top-level bullet / footnote statement is its own rule (they carry their
     own codes and outcomes), and consecutive prose statements are clubbed.
  4. Tables are NOT turned into one rule per row:
       - code lists (code + description)  -> Policy_Code_List sheet, attached to
                                              the rule that references them
       - LCD reference tables              -> codes + LCD ids attached to the
                                              NCD/LCD exception rule
       - worked examples (aaaaa, XXXXX, $) -> skipped
       - modifier tables                   -> one rule per modifier when the row
                                              carries its own instructions;
                                              otherwise clubbed into the
                                              section's rule as parameters
       - attribute tables with codes       -> one rule, per-code values in
                                              Rule_Code_Map
  5. Identical statements (after masking codes) are stated once per policy and
     their codes are unioned.
  6. Every rule gets a runnable SQL query on ONE claim-line table (see sql_skeleton.py).
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

INPUT_JSON = BASE_DIR / "extracted_rules.json" if (BASE_DIR / "extracted_rules.json").exists() else BASE_DIR / "humana_policies" / "extraction_rules.json"
OUTPUT_DIR = BASE_DIR
MIRROR_DIR = None
PAYER = "Humana"
LOB_LABEL = "Medicare Advantage"

POLICY_SLUG_MAP = {
    "Acupuncture for Chronic Lower Back Pain": "ACU",
    "Ambulance Services": "AMB",
    "Anesthesia Services": "ANES",
    "Assistant at Surgery Reimbursement": "ASST",
    "Common CPT and HCPCS Modifiers": "MOD",
    "DME for Medicare Advantage": "DMEMA",
    "DMEPOS Proof of Delivery Documentation": "DME",
    "Emergency Department E/M Reimbursement": "EDEM",
    "Multiple Procedure Payment Reduction (MPPR)": "MPPR",
    "Observation Services": "OBS",
    "Telehealth and Other Virtual Services": "TELE",
}

MARKER_RE = re.compile(r"^\+?([0-9A-Z]{5})(\*{1,6})?$")


# ------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------

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


def section_skipped(path: str) -> bool:
    root = (path or "").split(">")[0].strip()
    leaf = (path or "").split(">")[-1].strip()
    if SKIP_SECTION_RE.match(root) or SKIP_SECTION_RE.match(leaf):
        return True
    low = root.lower()
    if "medicaid" in low and "medicare" not in low:
        return True
    return False


def parse_table_header(content: str) -> str:
    m = re.match(r"\s*\[([^\]]+)\]", content or "")
    return clean_text(m.group(1)) if m else ""


def row_text(content: str) -> str:
    """Verbatim row text without the '[Header]' prefix and without web boilerplate ('Click here ...')."""
    txt = clean_text(re.sub(r"^\s*\[[^\]]+\]\s*", "", content or ""))
    return " ".join(s for s, _ in split_pieces(txt) if not BOILERPLATE_RE.search(s)).strip()


def stitch_policy(policy: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (sections, tables) in document order."""
    sections: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    for idx, b in enumerate(policy.get("extracted_content") or []):
        ctype = b.get("content_type") or "paragraph"
        path = clean_text(b.get("hierarchy_path") or b.get("title") or "")
        if ctype == "definition" or section_skipped(path):
            continue
        content = b.get("content") or ""
        page = b.get("page_number") or b.get("page_start")
        if ctype == "table_row":
            td = b.get("table_data") or {}
            cols = [clean_text(c) for c in (td.get("columns") or [])]
            row = {clean_text(k): clean_text(v) for k, v in (td.get("row") or {}).items()}
            header = parse_table_header(content)
            key = (path, header, tuple(cols))
            if tables and tables[-1]["key"] == key:
                tables[-1]["rows"].append(row)
                tables[-1]["texts"].append(row_text(content))
            else:
                tables.append({"key": key, "path": path, "header": header, "columns": cols, "rows": [row],
                               "texts": [row_text(content)], "order": idx, "page": page})
            continue
        text = clean_text(content)
        if not text:
            continue
        prev = sections[-1] if sections else None
        continuation = bool(prev) and prev["last_idx"] == idx - 1 and (
            prev["path"] == path
            or (path.startswith(prev["path"]) and re.match(r"^[▪•–◦]|^[a-z]", text)))
        if continuation and path != prev["path"]:
            # the extractor sometimes turns a list item into a heading and leaves an empty
            # bullet behind ("▪ ▪ Systemic ..."): put the item back where it came from
            leaf = path.split(">")[-1].strip()
            m = re.match(r"^([▪•◦–])\s+\1\s", text)
            if m and leaf:
                text = f"{m.group(1)} {leaf} {text[m.end() - 2:].lstrip()}"
        if continuation:
            sections[-1]["text"] += " " + text
            sections[-1]["last_idx"] = idx
        else:
            sections.append({"path": path, "text": text, "order": idx, "last_idx": idx, "page": page})
    return sections, tables


def classify_table(t: Dict[str, Any]) -> str:
    cols_l = [c.lower() for c in t["columns"]]
    header_l = t["header"].lower()
    texts = t["texts"]
    n = max(len(texts), 1)
    placeholder_rows = sum(1 for x in texts if PLACEHOLDER_CODE_RE.search(x) or "$" in x)
    real_code_rows = sum(1 for r in t["rows"] if any(extract_codes(v) for v in r.values()))
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
# main pipeline
# ------------------------------------------------------------------------------

def run_pipeline(input_json: Path, out_dir: Path, mirror_dir: Optional[Path]) -> None:
    print("=" * 80)
    print("HUMANA BILLING RULE GENERATION (Medicare Advantage)")
    print(f"Input: {input_json}")
    print("=" * 80)
    policies = json.loads(Path(input_json).read_text(encoding="utf-8"))
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
                      effective_date=clean_text(p.get("effective_date")), source_pdf=p.get("source_pdf") or "")
        rules: List[Rule] = []
        claimed: set = set()

        # ---- 1. prose sections ------------------------------------------------
        for sec in sections:
            for i, chunk in enumerate(select_rule_chunks(sec["text"], boiler=boiler, claimed=claimed, skip_claimed=False)):
                stype = "bullet" if re.match(r"^[•▪◦●–*]", chunk) else "prose"
                r = Rule(source_section=sec["path"], source_type=stype, text=chunk,
                         order=sec["order"] + i / 100.0, source_page=sec["page"], **common)
                r.add_codes(extract_codes(chunk), "rule text")
                rules.append(r)

        # ---- 2. tables ----------------------------------------------------------
        def anchor_for(t: Dict[str, Any], hints: List[str]) -> Optional[Rule]:
            same = [r for r in rules if r.source_section == t["path"] and r.source_type in ("prose", "bullet")]
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
        scope = [m.get("code") for m in (p.get("mapped_cpt_codes") or []) if is_code(m.get("code") or "")]
        scope = list(OrderedDict.fromkeys(scope))
        for r in rules:
            if not r.codes and scope and len(scope) <= 15:
                r.add_codes(scope, "policy scope (mapped_cpt_codes)")
        used_ids = set()
        for seq, r in enumerate(rules, 1):
            mod = r.extra.get("modifier_key", "")
            rid = f"HUM-{slug}-M{mod}" if mod and re.fullmatch(r"[A-Z0-9]{2}", mod) else f"HUM-{slug}-R{seq:02d}"
            while rid in used_ids:
                rid += "b"
            used_ids.add(rid)
            r.rule_id = rid
            finalize(r)
            if r.extra.get("modifier_row") and r.category not in ("MODIFIER_RULE", "PAYMENT_ADJUSTMENT"):
                r.secondary = [r.category] + [c for c in r.secondary if c != "MODIFIER_RULE"]
                r.category = "MODIFIER_RULE"
            if r.code_params:
                r.attachment_values.append(summarize_params(r.code_params))
        all_rules.extend(rules)

    for row in policy_code_list:
        row["Linked_Rule_ID"] = row.pop("_rule").rule_id if row.get("_rule") is not None else ""
        row.pop("_rule", None)

    records = [rule_record(r) for r in all_rules]
    extra_summary = [
        ("Corpus", "Payer", PAYER),
        ("Corpus", "Scope", "Medicare Advantage only"),
        ("Corpus", "Policies in input", len(policies)),
        ("Corpus", "Medicare policies processed", len(ma_policies)),
        ("Corpus", "Non-Medicare policies excluded", len(excluded)),
        ("Corpus", "Worked-example tables skipped (placeholder codes)", skipped_example_tables),
        ("Corpus", "Boilerplate threshold (policies sharing a sentence)", boiler.threshold()),
    ]
    pcl_sheet = ("Policy_Code_List",
                 ["Policy_Title", "Table", "Code", "Code_Description", "Footnote_Marker", "Linked_Rule_ID"],
                 policy_code_list, {"Policy_Title": 40, "Table": 34, "Code_Description": 60, "Linked_Rule_ID": 18},
                 {"Code_Description"})
    export_all(all_rules, records, out_dir, extra_summary, extra_sheets=[pcl_sheet],
               mirror_dirs=[mirror_dir] if mirror_dir and Path(mirror_dir).resolve() != Path(out_dir).resolve() else [],
               prefix="billing_rules_humana")

    from collections import Counter
    print(f"Medicare policies processed: {len(ma_policies)}  (excluded non-Medicare: {len(excluded)})")
    print(f"Rules generated: {len(all_rules)}   rules with table code lists: {sum(1 for r in all_rules if r.attachments)}")
    print(f"Worked-example tables skipped: {skipped_example_tables}")
    for c, n in Counter(r.category for r in all_rules).most_common():
        print(f"  {c:28s} {n:5d}")
    print(f"Outputs written to {out_dir}" + (f" and mirrored to {mirror_dir}" if mirror_dir else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Humana deterministic billing rule generator")
    ap.add_argument("--input", default=str(INPUT_JSON))
    ap.add_argument("--out-dir", default=str(OUTPUT_DIR))
    ap.add_argument("--mirror-dir", default="", help="second copy of outputs ('' to disable)")
    ap.add_argument("--sql-dialect", default=None,
                    choices=["postgres", "sqlserver", "oracle", "snowflake", "sqlite"],
                    help="SQL dialect for the generated queries (default: SQL_DIALECT in sql_skeleton.py)")
    a = ap.parse_args()
    if a.sql_dialect:
        import sql_skeleton
        sql_skeleton.SQL_DIALECT = a.sql_dialect
    run_pipeline(Path(a.input), Path(a.out_dir), Path(a.mirror_dir) if a.mirror_dir else None)


if __name__ == "__main__":
    main()
