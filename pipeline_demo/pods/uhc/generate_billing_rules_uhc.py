#!/usr/bin/env python3
"""
generate_billing_rules_uhc.py
=============================
UnitedHealthcare deterministic billing-rule generator (Commercial + Medicare Advantage).

Input : extraction_rules_uhc.json   (raw UHC policy JSON: reimbursement_guidelines,
                                     Question_Answer_Rules, linked_pdf_contents ...)
Output: billing_rules.{xlsx,json,csv} + billing_rules_code_map.csv

How rules are picked (keyword lexicon / grouping / clubbing live in rule_core.py):
  1. Each policy is read section by section. The parent "Reimbursement Guidelines"
     text repeats every subsection, so subsections are processed first and the
     parent only contributes text that is not inside a subsection -> no
     duplicate rules for the same sentence.
  2. A section becomes a rule only if it has a DIRECTIVE keyword. Short sections
     stay whole; long ones are cut only at complete-statement boundaries, so a
     rule always keeps its lead-in, bullet list and dependent follow-ups
     ("The other code is considered inclusive...", "No modifiers will override
     this denial.").
  3. Q&A entries are one rule each (question + answer, verbatim).
  4. ATTACHMENTS ARE NOT EXPLODED INTO RULES. Each UHC code-list attachment
     (Add-on to Primary, Time Span, Age-Based, Once-in-a-Lifetime, Bilateral
     Eligible, NDC max units ...) is parsed and attached to the ONE verbatim
     rule it belongs to:
        Target_Codes          -> every code in the attachment
        Attachment_Reference  -> the attachment file name
        Attachment_Values     -> compact per-value summary (e.g. Time Span:
                                 Calendar Month [43] | 30 days [40] ...)
        Rule_Code_Map sheet   -> one row per code / code-pair with its value
     CMS manuals and guides linked from a policy (clm104c*.pdf, bp102c*.pdf,
     Administrative Guide ...) are references, not code lists -> not parsed.
  5. Identical statements (after masking codes) are stated once per policy.
  6. Every rule gets a runnable SQL query on ONE claim-line table (see sql_skeleton.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

UHC_DIR = Path(__file__).resolve().parent
for _p in (UHC_DIR, UHC_DIR.parent):
    if (_p / "rule_core.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rule_core import (  # noqa: E402
    KNOWN_MODIFIERS, SKIP_SECTION_RE, CorpusBoilerplate, Rule, classify, clean_text, club_rules, norm_key,
    export_all, extract_codes, finalize, has_directive, is_code, rule_record, select_rule_chunks,
    summarize_params,
)

PAYER = "UnitedHealthcare"

# ------------------------------------------------------------------------------
# OUTPUT: only billing_rule_<client>.xlsx
# ------------------------------------------------------------------------------
import shutil
import tempfile

CLIENT = "uhc"
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



# ------------------------------------------------------------------------------
# data-quality guards (UHC only; shared rule_core / sql_skeleton are untouched)
# ------------------------------------------------------------------------------
import datetime as _dt
from rule_core import _MOD_PROHIBIT_RE, _MOD_RANGE_RE  # noqa: E402


def normalize_date(v: str) -> str:
    """'3/1/2026' / '03/01/26' / '2026-03-01' -> '2026-03-01' (one format for every payer)."""
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return _dt.datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return v


# "Modifier 22 should not be appended to an E/M service" / "the 50 modifier should not be reported" restrict WHERE
# the modifier is used (E/M, bilateral codes ...); they do not ban the modifier. rule_core only recognises the
# verb-first wording ("Do not report modifier 22 with ..."), so the subject-first wording flagged every line
# carrying the modifier.
_VERB_RE = re.compile(r"\bnot\s+be\s+(?:reported|appended|used|billed|added)\b", re.I)
_QUALIFIER_RE = re.compile(r"\s*(?:on|with|for|to|after|when|in|if|unless|by)\b", re.I)


def _is_mod_in(m: str, text: str) -> bool:
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(m)}(?![A-Z0-9])", text))


def drop_context_prohibitions(r: Rule) -> None:
    """Keep a modifier prohibited only when a sentence really bans it:
      * 'Modifiers LT and RT should not be reported when the 50 modifier applies' -> LT, RT stay prohibited
        (the qualifier names another modifier); 50 is only the qualifier;
      * 'Modifier 22 should not be appended to an E/M service' -> a restriction on WHERE 22 is used, not a ban."""
    keep = []
    # "P1 – P6" counts as P1, P2 ... P6, exactly as rule_core expands it
    text = _MOD_RANGE_RE.sub(lambda mo: ", ".join(f"{mo.group(1)}{i}" for i in range(int(mo.group(2)), int(mo.group(3)) + 1)), r.text)
    for m in r.mods_prohibited:
        ban = False
        for sent in re.split(r"(?<=[.;:])\s+|\s(?=[•▪])", text):
            if not (_is_mod_in(m, sent) and _MOD_PROHIBIT_RE.search(sent)):
                continue
            v = _VERB_RE.search(sent)
            lead = bool(re.match(r"\s*(?:if|when|unless)\b", sent, re.I))
            if not v:
                ban = True                       # another wording (not recognized, informational only ...): unchanged
                break
            before, after = sent[:v.start()], sent[v.end():]
            if not _is_mod_in(m, before):
                continue                         # m is only named in the qualifier
            qualifier = bool(_QUALIFIER_RE.match(after))
            names_other_mod = any(_is_mod_in(o, after) for o in r.modifiers if o != m)
            if qualifier and not names_other_mod:
                continue                         # restriction to a service / condition, not a ban
            if lead and not names_other_mod:
                continue
            ban = True
            break
        if ban:
            keep.append(m)
    r.mods_prohibited = keep


# ==============================================================================
# ATTACHMENT PARSING
# ==============================================================================

def att_name(att: Dict[str, Any]) -> str:
    return (att.get("url") or "").rstrip("/").rsplit("/", 1)[-1] or "attachment"


def att_text(att: Dict[str, Any]) -> str:
    return "\n".join(c.get("content") or "" for c in att.get("content") or [])


def att_lines(att: Dict[str, Any]) -> List[str]:
    return [l.strip() for l in att_text(att).split("\n") if l.strip()]


_TOK: Dict[str, Callable[[str], bool]] = {
    "C": lambda t: is_code(t),
    "D": lambda t: bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{8}", t)),
    "N": lambda t: bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t)),
    "P": lambda t: bool(re.fullmatch(r"-?\d+(?:\.\d+)?%", t)),
    "A": lambda t: bool(re.fullmatch(r"[YMDW]", t)),
    "M": lambda t: t in KNOWN_MODIFIERS and not is_code(t),
    "T": lambda t: not is_code(t),
}


def parse_stream(lines: List[str], schema: List[Tuple[str, str, bool]]) -> List[Dict[str, str]]:
    """Rebuild table rows from a PDF text stream (one cell per line).
    schema = [(token_type, column_name, optional), ...]; resyncs on misaligned tokens."""
    rows, i, n = [], 0, len(lines)
    while i < n and not _TOK[schema[0][0]](lines[i]):
        i += 1
    while i < n:
        start, row, ok = i, {}, True
        for typ, name, opt in schema:
            if i < n and _TOK[typ](lines[i]):
                row[name] = lines[i]; i += 1
            elif opt:
                row[name] = ""
            else:
                ok = False; break
        if ok:
            rows.append(row)
        else:
            i = start + 1
    return rows


def table_rows(att: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, str]]]:
    """Rows from extracted PDF tables. Tables after page 1 often use their first data row
    as column names - those are detected and put back as data rows."""
    tables = att.get("tables") or []
    if not tables:
        return [], []
    canon = [clean_text(c) for c in (tables[0].get("columns") or [])]
    rows = []
    for t in tables:
        cols = [clean_text(c) for c in (t.get("columns") or [])]
        if cols != canon and len(cols) == len(canon):
            looks_data = any(is_code(c) or re.fullmatch(r"[\d.,%/]+|Column \d+", c) for c in cols)
            if looks_data:
                rows.append({canon[k]: ("" if re.fullmatch(r"Column \d+", c) else c) for k, c in enumerate(cols)})
        for r in t.get("rows") or []:
            vals = [clean_text(r.get(c, "")) for c in (t.get("columns") or [])]
            if len(vals) == len(canon):
                rows.append({canon[k]: ("" if re.fullmatch(r"Column \d+", v) else v) for k, v in enumerate(vals)})
            else:
                rows.append({clean_text(k): clean_text(v) for k, v in r.items()})
    return canon, rows


def P(code: str, name: str = "", value: str = "", related: str = "", rel: str = "", source: str = "") -> Dict[str, str]:
    return {"code": code, "parameter_name": name, "parameter_value": value, "related_code": related,
            "relationship": rel, "source": source}


def _days(v: str) -> str:
    return f"{v} days" if v.isdigit() else v


# --- one parser per known UHC list --------------------------------------------
# each returns (target_codes, code_params, modifiers)

def p_time_span(att):
    rows = parse_stream(att_lines(att), [("C", "Code", False), ("T", "Time Span", False)])
    return [r["Code"] for r in rows], [P(r["Code"], "Time Span", _days(r["Time Span"]), source=att_name(att)) for r in rows], []


def p_comp_component(att):
    _, rows = table_rows(att)
    pairs = [(r.get("Code"), r.get("Code 2")) for r in rows if is_code(r.get("Code", "")) and is_code(r.get("Code 2", ""))]
    if not pairs:
        rows = parse_stream(att_lines(att), [("C", "a", False), ("C", "b", False)])
        pairs = [(r["a"], r["b"]) for r in rows]
    codes = list(OrderedDict.fromkeys([c for pr in pairs for c in pr]))
    return codes, [P(a, related=b, rel="COMPREHENSIVE_TO_COMPONENT", source=att_name(att)) for a, b in pairs], []


def p_addon_primary_first(att):  # PRIMARY_CODE | ADD_ON_CODE | EFFECTIVE_DATE | EXPIRATION_DATE
    rows = parse_stream(att_lines(att), [("C", "primary", False), ("C", "addon", False), ("D", "eff", True), ("D", "exp", True)])
    params = [P(r["addon"], "Effective Date" if r["eff"] else "", r["eff"] + (f" – {r['exp']}" if r["exp"] else ""),
                related=r["primary"], rel="ADD_ON_TO_PRIMARY", source=att_name(att)) for r in rows]
    return list(OrderedDict.fromkeys(r["addon"] for r in rows)), params, []


def p_addon_addon_first(att):  # Add-on Code | Primary Code
    rows = parse_stream(att_lines(att), [("C", "addon", False), ("C", "primary", False)])
    params = [P(r["addon"], related=r["primary"], rel="ADD_ON_TO_PRIMARY", source=att_name(att)) for r in rows]
    return list(OrderedDict.fromkeys(r["addon"] for r in rows)), params, []


def p_addon_auto(att):
    head = " ".join(att_lines(att)[:8]).lower().replace("\n", "")
    if head.find("primary") != -1 and (head.find("add") == -1 or head.find("primary") < head.find("add")):
        return p_addon_primary_first(att)
    return p_addon_addon_first(att)


def p_age(att):
    rows = parse_stream(att_lines(att), [("C", "Code", False), ("N", "b", False), ("A", "bt", False),
                                         ("N", "e", False), ("A", "et", False)])
    unit = {"Y": "years", "M": "months", "D": "days", "W": "weeks"}
    params = [P(r["Code"], "Age Range", f"{r['b']} {unit[r['bt']]} – {r['e']} {unit[r['et']]}", source=att_name(att))
              for r in rows]
    return [r["Code"] for r in rows], params, []


def p_lifetime(att):
    lines = att_lines(att)
    try:
        i = next(k for k, l in enumerate(lines) if l.strip().lower() == "code") + 1
    except StopIteration:
        i = 0
    fam, codes, params = "", [], []
    for l in lines[i:]:
        if is_code(l):
            codes.append(l)
            params.append(P(l, "Code Family", fam, source=att_name(att)))
        elif not re.fullmatch(r"(?i)family|code|page \d+.*", l):
            fam = l
    return codes, params, []


def p_code_value(param_name):
    def _p(att):
        rows = parse_stream(att_lines(att), [("C", "Code", False), ("T", "v", False)])
        return [r["Code"] for r in rows], [P(r["Code"], param_name, r["v"], source=att_name(att)) for r in rows], []
    return _p


def p_code_list(att):
    codes = list(OrderedDict.fromkeys(l for l in att_lines(att) if is_code(l)))
    if not codes:
        codes = extract_codes(att_text(att))
    return codes, [], []


def p_code_date(att):
    rows = parse_stream(att_lines(att), [("C", "Code", False), ("C", "Code2", True), ("D", "Date", True)])
    return (list(OrderedDict.fromkeys(r["Code"] for r in rows)),
            [P(r["Code"], "Effective Date" if r["Date"] else "", r["Date"], source=att_name(att)) for r in rows], [])


def p_gap_fill(att):
    rows = parse_stream(att_lines(att), [("C", "Code", False), ("M", "Mod", True), ("P", "Pct", False),
                                         ("N", "Split", False), ("P", "Var", False), ("N", "RVU", False)])
    params = [P(r["Code"], f"RBRVS % Split ({r['Mod'] or 'global'})", r["Pct"], source=att_name(att)) for r in rows]
    return list(OrderedDict.fromkeys(r["Code"] for r in rows)), params, []


def p_modifier_list(att):
    mods = [l for l in att_lines(att) if l in KNOWN_MODIFIERS]
    return [], [], list(OrderedDict.fromkeys(mods))


def p_ndc(att):
    cols, rows = table_rows(att)
    jcol = next((c for c in cols if re.search(r"j\s?code|hcpcs", c, re.I)), None)
    ncol = next((c for c in cols if "ndc" in c.lower()), None)
    lcols = [c for c in cols if "limit" in c.lower()]
    mcol = next((c for c in cols if "multiple" in c.lower()), None)
    dcol = next((c for c in cols if "drug" in c.lower()), None)
    codes, params = [], []
    for r in rows:
        j = (r.get(jcol) or "").strip() if jcol else ""
        if not is_code(j):
            continue
        codes.append(j)
        limits = "/".join(r.get(c) for c in lcols if r.get(c))
        extra = "; ".join(x for x in (f"drug {r.get(dcol)}" if dcol and r.get(dcol) else "",
                                      f"multiple site {r.get(mcol)}" if mcol and r.get(mcol) else "") if x)
        params.append(P(j, "Max Units per NDC package", limits + (f" ({extra})" if extra else ""),
                        related=r.get(ncol, "") if ncol else "", rel="NDC11", source=att_name(att)))
    return list(OrderedDict.fromkeys(codes)), params, []


def p_tables_generic(att):
    """Any table that has a code column: codes + the first other column as parameter."""
    cols, rows = table_rows(att)
    codes, params = [], []
    for r in rows:
        code_cols = [c for c in cols if extract_codes(r.get(c, "")) and re.search(r"code|cpt|hcpcs|procedure", c, re.I)]
        if not code_cols:
            continue
        other = [c for c in cols if c not in code_cols and r.get(c)]
        for cc in code_cols:
            for code in extract_codes(r.get(cc, "")):
                codes.append(code)
                if other:
                    params.append(P(code, other[0], r.get(other[0], ""), source=att_name(att)))
    return list(OrderedDict.fromkeys(codes)), params, []


# (filename regex, kind, category of the anchored rule, parser, anchor hint phrases)
ATTACHMENT_SPECS: List[Tuple[str, str, Optional[str], Callable, List[str]]] = [
    (r"timespancomprehensive|comprehensive.?and.?component", "COMPREHENSIVE_COMPONENT", "BUNDLING_INCLUSION",
     p_comp_component, ["share a common portion", "most comprehensive", "comprehensive", "inclusive"]),
    (r"time.?span", "TIME_SPAN", "TIME_SPAN_LIMIT", p_time_span,
     ["specifies a time period", "once during that time period", "time span", "time period"]),
    (r"add-?on", "ADD_ON", "ADD_ON_PRIMARY", p_addon_auto,
     ["add-on codes are never reimbursed unless", "must be reported with the primary", "primary procedure", "add-on"]),
    (r"age-?based", "AGE", "AGE_GENDER_RESTRICTION", p_age,
     ["within the age designation", "age designation", "age range", "age"]),
    (r"once-?in-?a-?lifetime", "LIFETIME", "ONCE_IN_A_LIFETIME", p_lifetime,
     ["code family", "once during a patient", "lifetime"]),
    (r"bilateral-?eligible", "BILATERAL_ELIGIBLE", "MODIFIER_RULE", p_code_value("Bilateral Indicator"),
     ["bilateral eligible procedures policy list", "listed on the", "modifier 50", "bilateral"]),
    (r"bilat-?desc", "BILATERAL_IN_DESCRIPTION", "MODIFIER_RULE", p_code_list,
     ["written in the description", "unilateral or bilateral", "bilateral"]),
    (r"assistant-?at-?surgery|assistant-?surgeon", "ASSISTANT_SURGEON_ELIGIBLE", "MODIFIER_RULE", p_code_list,
     ["assistant surgeon eligible", "eligible", "assistant"]),
    (r"co-?surgeon", "CO_SURGEON_ELIGIBLE", "MODIFIER_RULE", p_code_list, ["co-surgeon eligible", "co-surgeon", "modifier 62"]),
    (r"team-?surgeon", "TEAM_SURGEON_ELIGIBLE", "MODIFIER_RULE", p_code_list, ["team surgeon eligible", "team surgeon", "modifier 66"]),
    (r"ndc", "NDC_MAX_UNITS", "FREQUENCY_LIMIT", p_ndc,
     ["maximum units allowed per package", "package maximum", "maximum units", "ndc"]),
    (r"gap-?fill", "PC_TC_GAP_FILL", "PAYMENT_ADJUSTMENT", p_gap_fill,
     ["gap", "split", "professional component", "technical component"]),
    (r"radiology", "RADIOLOGY_CODES", None, p_code_date, ["radiology", "interpretation", "radiology code list"]),
    (r"modifiers?-?list", "MODIFIER_LIST", "MODIFIER_RULE", p_modifier_list, ["modifier", "modifiers"]),
    (r"ambulance", "AMBULANCE_BUNDLED", "BUNDLING_INCLUSION", p_code_list, ["bundled", "ambulance", "inclusive"]),
    (r"emergency-?department|edc", "ED_VISIT_LEVELS", None, p_tables_generic,
     ["emergency department claim", "edc analyzer", "optum", "visit level"]),
    (r"em-?code-?list", "EM_CODES", None, p_code_list, ["evaluation and management", "e/m"]),
    (r"new-?patient", "NEW_PATIENT_CODES", None, p_code_list, ["new patient", "initial visit"]),
]
REFERENCE_DOC_RE = re.compile(r"^(?:clm|bp)\d+c\d+\.pdf$|administrative-guide|molecular-pathology-policy|"
                              r"proper-use-modifiers|manual", re.I)


def parse_attachment(att: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = att_name(att)
    if REFERENCE_DOC_RE.search(name):
        return {"name": name, "kind": "REFERENCE", "codes": [], "params": [], "mods": [], "cat": None, "hints": []}
    for rx, kind, cat, parser, hints in ATTACHMENT_SPECS:
        if re.search(rx, name, re.I):
            codes, params, mods = parser(att)
            return {"name": name, "kind": kind, "codes": codes, "params": params, "mods": mods, "cat": cat, "hints": hints}
    # unknown attachment: code list if it is mostly codes, else reference
    lines = att_lines(att)
    density = sum(1 for l in lines if is_code(l)) / max(len(lines), 1)
    if density >= 0.3:
        codes, params, mods = p_code_list(att)
        return {"name": name, "kind": "CODE_LIST", "codes": codes, "params": params, "mods": mods, "cat": None, "hints": []}
    codes, params, mods = p_tables_generic(att)
    if codes:
        return {"name": name, "kind": "CODE_TABLE", "codes": codes, "params": params, "mods": mods, "cat": None, "hints": []}
    return {"name": name, "kind": "REFERENCE", "codes": [], "params": [], "mods": [], "cat": None, "hints": []}


def list_name_phrases(att: Dict[str, Any], name: str) -> List[str]:
    """How the policy text names this list: from the file name and the attachment's first line.
    'Team-Surgeon-Eligible-Policy-List.pdf' -> 'team surgeon eligible'."""
    out = []
    for raw in (name.rsplit(".", 1)[0], (att_lines(att)[:1] or [""])[0]):
        ph = re.sub(r"[-_]+", " ", raw).lower()
        ph = re.sub(r"\b(?:medadv|policy|list|codes?|proc|rltnshp|\d{4})\b", " ", ph)
        ph = re.sub(r"\s+", " ", ph).strip()
        if len(ph.split()) >= 2:
            out.append(ph)
    return list(OrderedDict.fromkeys(out))


def _norm_phrase_text(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", t.lower()))


def attachment_title_words(att: Dict[str, Any], name: str) -> List[str]:
    first = (att_lines(att)[:1] or [""])[0]
    words = re.findall(r"[a-z]{4,}", (name.rsplit(".", 1)[0].replace("-", " ").replace("_", " ") + " " + first).lower())
    stop = {"policy", "list", "codes", "code", "medadv", "with", "desc", "procedures", "procedure", "rltnshp", "proc"}
    return list(OrderedDict.fromkeys(w for w in words if w not in stop))


# ==============================================================================
# POLICY -> TEXT UNITS
# ==============================================================================

def policy_units(policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    rg = policy.get("reimbursement_guidelines") or {}
    units: List[Dict[str, Any]] = []
    subs = rg.get("subsections") or []
    parent = clean_text(rg.get("content"))
    order = 0
    for s in subs:
        title = clean_text(s.get("title"))
        content = clean_text(s.get("content"))
        # NOTE: the parent text repeats every subsection. Nothing is cut out of the parent here
        # (cutting corrupts sentences); the parent is processed LAST and every sentence already
        # taken from a subsection - including one with the subsection heading glued in front of
        # it - is skipped by the 'claimed' check in rule_core.select_rule_chunks.
        order += 1
        if SKIP_SECTION_RE.match(title or ""):
            continue
        units.append({"section": f"Reimbursement Guidelines > {title}" if title else "Reimbursement Guidelines",
                      "text": content, "type": "prose", "order": order, "tables": s.get("tables") or []})
    # Some policies keep only the lead-in sentence in the subsection ("... for any of the conditions
    # listed below:") while the "Procedure Code(s) ... Condition(s) a. ... b. ..." block sits in the
    # PARENT text under the same heading. Cut the parent into heading segments (headings matched at
    # line starts in the raw text) and complete each subsection with its segment, verbatim.
    raw_parent = rg.get("content") or ""
    heads = []
    last = 0
    for u in units:
        t = u["section"].split(">", 1)[-1].strip()
        if not t or u["section"] == "Reimbursement Guidelines":
            continue
        words = r"\s+".join(map(re.escape, t.split()))
        m = (re.compile(r"(?:^|\n)[ \t]*" + words + r"[ \t]*(?=\n|$)").search(raw_parent, last)
             or re.compile(r"(?:^|(?<=\s))" + words + r"(?=\s{2,}|\s*\n|\s+Procedure Code)").search(raw_parent, last))
        if m:
            heads.append((m.start(), m.end(), u))
            last = m.end()
    for i, (hs, he, u) in enumerate(heads):
        seg = clean_text(raw_parent[he: heads[i + 1][0] if i + 1 < len(heads) else len(raw_parent)])
        if not seg:
            continue
        sub = u["text"]
        # only complete a subsection that points at a list it does not contain
        needs_list = (not sub) or sub.rstrip().endswith(":") or bool(re.search(
            r"listed below|the following|described below|as follows|below:", sub, re.I)) and not extract_codes(sub)
        if not needs_list:
            continue
        if sub and norm_key(sub) in norm_key(seg):
            u["text"] = seg                      # segment already holds lead-in + list
        elif sub and norm_key(seg) in norm_key(sub):
            continue                             # subsection already complete
        else:
            u["text"] = (sub + " " + seg).strip() if sub else seg
        u["completed_from_parent"] = True
    # the parent keeps only its own preamble (text before the first subsection heading);
    # everything under a heading now belongs to that subsection
    if heads:
        parent = raw_parent[:heads[0][0]]
    parent = clean_text(parent)
    units.insert(0, {"section": "Reimbursement Guidelines", "text": parent, "type": "prose", "order": 0,
                     "tables": rg.get("tables") or [], "is_parent": True})
    qa_order = 500
    for blk in policy.get("Question_Answer_Rules") or []:
        if not isinstance(blk, dict):
            continue
        for label, qa in blk.items():
            if not isinstance(qa, dict):
                continue
            q, a = clean_text(qa.get("Q")), clean_text(qa.get("A"))
            if a:
                qa_order += 1
                units.append({"section": f"Questions and Answers > {label}", "text": f"Q: {q} A: {a}",
                              "answer": a, "type": "qa", "order": qa_order, "tables": []})
    return units


def inline_table_pairs(tables: List[Any]) -> List[Tuple[str, str]]:
    pairs = []
    for t in tables or []:
        if isinstance(t, dict):
            for kv in t.get("content") or []:
                if isinstance(kv, dict):
                    pairs.append((clean_text(kv.get("key")), clean_text(kv.get("value"))))
    return pairs


# ==============================================================================
# ENGINE
# ==============================================================================

_LIST_LEADIN_RE = re.compile(r"\b(?:the following|listed below|below|code\(s\)|codes? listed)\b", re.I)


def _pick_list_rule(cands: List[Rule], tcodes: Optional[List[str]] = None) -> Rule:
    """The rule that introduces a code list: shares a code with it, else 'the following ... code(s)'
    without codes of its own, else the first rule of the section."""
    if tcodes:
        hit = [r for r in cands if set(tcodes) & set(r.codes)]
        if hit:
            return hit[0]
    lead = [r for r in cands if _LIST_LEADIN_RE.search(r.text) and not r.codes]
    return (lead or cands)[0]


def _closest_code_family(tcodes: List[str], rules: List[Rule]) -> Optional[Rule]:
    exact = [r for r in rules if set(tcodes) <= set(r.codes)]
    if exact:
        return exact[0]  # table only repeats codes a rule already states
    best, best_len = None, 2
    for r in rules:
        for a in tcodes:
            for b in r.codes:
                n = 0
                while n < min(len(a), len(b)) and a[n] == b[n]:
                    n += 1
                if n > best_len:
                    best, best_len = r, n
    return best


def lob_code(lob: str) -> str:
    return "MA" if "medicare" in lob.lower() else ("COM" if "commercial" in lob.lower() else re.sub(r"\W", "", lob)[:4].upper())


def run(input_path: Path, out_dir: Path, lob_filter: str, compare_dir: Optional[Path] = None) -> None:
    print("=" * 80)
    print("UNITEDHEALTHCARE BILLING RULE GENERATION")
    print(f"Input: {input_path}")
    print("=" * 80)
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    print(f"Loaded {len(raw)} policy objects.")

    # ---- select + de-duplicate policies -----------------------------------------
    policies, seen, skipped_empty, skipped_lob, dup = [], set(), 0, 0, 0
    for p in raw:
        title = clean_text(p.get("policy_title"))
        rg = p.get("reimbursement_guidelines") or {}
        has_content = bool(clean_text(rg.get("content")) or rg.get("subsections") or p.get("Question_Answer_Rules"))
        if not title or not has_content:
            skipped_empty += 1
            continue
        lob = clean_text(p.get("Line_of_Business")) or "Medicare Advantage"
        if lob_filter != "all" and lob_code(lob).lower() != lob_filter.lower():
            skipped_lob += 1
            continue
        key = (title, clean_text(p.get("policy_number")), lob)
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        policies.append(p)

    boiler = CorpusBoilerplate()
    units_by_policy = []
    for p in policies:
        units = policy_units(p)
        units_by_policy.append(units)
        boiler.add_policy(u["text"] for u in units if u["type"] == "prose")

    all_rules: List[Rule] = []
    reference_docs: List[Dict[str, Any]] = []
    unanchored = 0

    for p, units in zip(policies, units_by_policy):
        title = clean_text(p.get("policy_title"))
        pnum = clean_text(p.get("policy_number"))
        lob = clean_text(p.get("Line_of_Business")) or "Medicare Advantage"
        bclass = clean_text(p.get("Billingclass")) or "Both"
        common = dict(payer=PAYER, policy_key=f"{title}|{pnum}|{lob}", policy_title=title, policy_number=pnum,
                      line_of_business=lob, billing_class=bclass, effective_date=normalize_date(clean_text(p.get("effective_date"))))
        rules: List[Rule] = []
        claimed: set = set()

        # ---- 1. prose: subsections first, then parent leftovers ------------------
        sub_titles = sorted({clean_text(x.get("title")) for x in (p.get("reimbursement_guidelines") or {}).get("subsections") or []
                             if clean_text(x.get("title"))}, key=len, reverse=True)
        ordered = [u for u in units if u["type"] == "prose" and not u.get("is_parent")] + \
                  [u for u in units if u.get("is_parent")]
        for u in ordered:
            unit_rules = []
            heads = sub_titles if u.get("is_parent") else None
            # sibling sections may legitimately repeat a sentence; only the parent (which repeats
            # every subsection) skips sentences already used
            for i, chunk in enumerate(select_rule_chunks(u["text"], boiler=boiler, claimed=claimed, headings=heads,
                                                         skip_claimed=bool(u.get("is_parent")))):
                r = Rule(source_section=u["section"], source_type="prose", text=chunk,
                         order=u["order"] + i / 100.0, **common)
                r.add_codes(extract_codes(chunk), "rule text")
                unit_rules.append(r)
            rules.extend(unit_rules)
            u["_rules"] = unit_rules

        # inline key/value tables (section tables -> that section's rule; parent tables -> closest code family)
        for u in ordered:
            for tbl in u["tables"]:
                pairs = inline_table_pairs([tbl])
                cells = " ".join(f"{k} {v}".strip() for k, v in pairs)
                # a lead-in ending with ':' right before this table gets the table cells (verbatim)
                lead = [r for r in u.get("_rules") or [] if r.text.rstrip().endswith(":")]
                if lead and cells:
                    lead[-1].text = lead[-1].text.rstrip() + " " + cells
                mod_cells = [x for x in cells.split() if x in KNOWN_MODIFIERS and not is_code(x)]
                if lead and mod_cells and len(mod_cells) == len(cells.split()):
                    lead[-1].add_modifiers(mod_cells)
                    continue
                code_pairs = [(k, v) for k, v in pairs if is_code(k)]
                if not code_pairs or len(code_pairs) < 0.5 * len(pairs):
                    continue
                label = f"Table in {u['section'].split('>')[-1].strip()}"
                tcodes = [k for k, _ in code_pairs] + [c for _, v in code_pairs for c in extract_codes(v)]
                if lead:
                    tgt = lead[-1]
                elif u.get("is_parent") or not u.get("_rules"):
                    tgt = _closest_code_family(tcodes, rules)
                else:
                    tgt = _pick_list_rule(u["_rules"], tcodes)
                if tgt is None:
                    tgt = Rule(source_section=u["section"], source_type="table_only",
                               text=" | ".join(f"{k}: {v}" for k, v in pairs), order=u["order"], **common)
                    rules.append(tgt)
                grid = all(extract_codes(v) and not re.sub(r"[\s,;+]|" + r"|".join(extract_codes(v)), "", v)
                           for _, v in code_pairs)
                for k, v in code_pairs:
                    if grid:
                        # code grid laid out as a 2-column table: every cell is just a code
                        tgt.add_codes([k] + extract_codes(v), label)
                    else:
                        tgt.add_codes([k], label)
                        tgt.code_params.append(P(k, "Table value", v, source=label))
                if label not in tgt.attachments:
                    tgt.attachments.append(label)

        # ---- 2. Q&A (one verbatim rule per Q&A) ----------------------------------
        for u in units:
            if u["type"] != "qa" or not has_directive(u["answer"]):
                continue
            r = Rule(source_section=u["section"], source_type="qa", text=u["text"], order=u["order"], **common)
            r.add_codes(extract_codes(u["text"]), "rule text")
            rules.append(r)

        # ---- 3. attachments -> anchored to ONE verbatim rule ---------------------
        guess = {id(r): classify(r.text)[0] for r in rules}
        for att in p.get("linked_pdf_contents") or []:
            info = parse_attachment(att)
            name = info["name"]
            if info["kind"] == "REFERENCE":
                reference_docs.append({"Policy_Title": title, "Line_of_Business": lob, "Attachment": name,
                                       "URL": att.get("url", ""), "Handling": "Reference document (CMS manual / guide) - "
                                       "not a code list; not parsed into rule codes"})
                continue
            if not info["codes"] and not info["mods"]:
                reference_docs.append({"Policy_Title": title, "Line_of_Business": lob, "Attachment": name,
                                       "URL": att.get("url", ""), "Handling": "No codes could be parsed"})
                continue
            words = attachment_title_words(att, name)
            phrases = list_name_phrases(att, name)

            def score(r: Rule) -> float:
                t = r.text.lower()
                s = 0.0
                for i, h in enumerate(info["hints"]):
                    if h in t:
                        s += 4 if i == 0 else 2
                s += 0.5 * sum(1 for w in words if w in t)
                if any(ph in _norm_phrase_text(r.text) for ph in phrases):
                    s += 5
                if re.search(r"\blist\b|attachment", t):
                    s += 1
                if info["cat"] and guess.get(id(r)) == info["cat"]:
                    s += 2
                if r.source_type == "qa":
                    s -= 2
                return s

            ranked = sorted(((score(r), -r.order, r) for r in rules if r.source_type in ("prose", "qa")),
                            key=lambda x: (x[0], x[1]), reverse=True)
            anchor = ranked[0][2] if ranked and ranked[0][0] >= 3 else None
            if anchor is None:
                unanchored += 1
                anchor = Rule(source_section=f"Attachment: {name}", source_type="attachment_only",
                              text=f"[Attachment: {name}]", order=900, **common)
                rules.append(anchor)
            anchor.add_codes(info["codes"], f"attachment: {name}")
            anchor.add_modifiers(info["mods"])
            anchor.code_params.extend(info["params"])
            # tag plain list codes with the list they came from (one rule can carry several lists)
            with_param = {x["code"] for x in info["params"]}
            anchor.code_params.extend(P(c, source=name) for c in info["codes"] if c not in with_param)
            if name not in anchor.attachments:
                anchor.attachments.append(name)
            summ = summarize_params(info["params"]) if info["params"] else ""
            if info["mods"] and not info["codes"]:
                summ = "Modifiers: " + ", ".join(info["mods"])
            anchor.attachment_values.append(f"{name}: {len(info['codes'])} codes" + (f" — {summ}" if summ else ""))
            if info["cat"] and not anchor.forced_category:
                anchor.forced_category = info["cat"]
            # other rules that name the same list ("... on the Assistant-at-Surgery Eligible List ...")
            # carry the same codes - the rule is stated once, the codes go wherever the list is cited
            for other in rules:
                if other is anchor or other.source_type not in ("prose", "qa"):
                    continue
                if any(re.search(re.escape(ph) + r"(?:\s+\w+){0,3}\s+lists?\b", _norm_phrase_text(other.text))
                       for ph in phrases):
                    other.add_codes(info["codes"], f"attachment: {name}")
                    if name not in other.attachments:
                        other.attachments.append(name)
                        other.attachment_values.append(f"{name}: {len(info['codes'])} codes (list cited in rule; "
                                                       f"per-code values on {anchor.rule_id or 'the anchor rule'})")

        # ---- 4. club + ids ---------------------------------------------------------
        rules = club_rules(sorted(rules, key=lambda r: r.order))
        universe = [c for c in (p.get("CPT_CODES_USED") or []) if is_code(str(c))]
        for seq, r in enumerate(rules, 1):
            if not r.codes and universe and len(universe) <= 10:
                r.add_codes(universe, "policy scope (CPT_CODES_USED)")
            r.rule_id = f"UHC-{lob_code(lob)}-{pnum or 'NA'}-R{seq:02d}"
            finalize(r)
            drop_context_prohibitions(r)
            # inline-table values summarised like attachment values
            tbl_params = [x for x in r.code_params if x.get("source", "").startswith("Table in")]
            if tbl_params:
                r.attachment_values.append(summarize_params(tbl_params))
        all_rules.extend(rules)

    records = [rule_record(r) for r in all_rules]

    extra_summary = [
        ("Corpus", "Payer", PAYER),
        ("Corpus", "Policies in input", len(raw)),
        ("Corpus", "Policies processed", len(policies)),
        ("Corpus", "Skipped: no title / empty guidelines", skipped_empty),
        ("Corpus", f"Skipped: LOB filter ({lob_filter})", skipped_lob),
        ("Corpus", "Skipped: duplicate policy entries", dup),
        ("Corpus", "Attachments treated as reference documents", sum(1 for d in reference_docs)),
        ("Corpus", "Attachments with no matching prose rule", unanchored),
        ("Corpus", "Boilerplate threshold (policies sharing a sentence)", boiler.threshold()),
    ]
    ref_sheet = ("Reference_Documents", ["Policy_Title", "Line_of_Business", "Attachment", "URL", "Handling"],
                 reference_docs, {"Policy_Title": 40, "Attachment": 40, "URL": 60, "Handling": 60}, {"Handling"})
    written = export_xlsx_only(
        lambda d: export_all(all_rules, records, d, extra_summary, extra_sheets=[ref_sheet]), out_dir, compare_dir)

    print(f"Policies processed: {len(policies)} (skipped empty {skipped_empty}, dup {dup}, lob {skipped_lob})")
    print(f"Rules generated: {len(all_rules)}   with attachment/table codes: {sum(1 for r in all_rules if r.attachments)}"
          f"   attachments without prose anchor: {unanchored}")
    for c, n in Counter(r.category for r in all_rules).most_common():
        print(f"  {c:28s} {n:5d}")
    for w in written:
        print(f"Written: {w}")


def main() -> None:
    ap = argparse.ArgumentParser(description="UHC deterministic billing rule generator")
    ap.add_argument("--input", default=str(UHC_DIR / "extraction_rules_uhc.json"))
    ap.add_argument("--out-dir", default=str(UHC_DIR))
    ap.add_argument("--compare-dir", default=str(COMPARE_DIR), help="Rule Comparison folder ('' to disable)")
    ap.add_argument("--lob", default="all", choices=["all", "ma", "com"],
                    help="all (default) | ma = Medicare Advantage only | com = Commercial only")
    ap.add_argument("--sql-dialect", default=None,
                    choices=["postgres", "sqlserver", "oracle", "snowflake", "sqlite"],
                    help="SQL dialect for the generated queries (default: SQL_DIALECT in sql_skeleton.py)")
    a = ap.parse_args()
    if a.sql_dialect:
        import sql_skeleton
        sql_skeleton.SQL_DIALECT = a.sql_dialect
    run(Path(a.input), Path(a.out_dir), a.lob, Path(a.compare_dir) if a.compare_dir else None)


if __name__ == "__main__":
    main()