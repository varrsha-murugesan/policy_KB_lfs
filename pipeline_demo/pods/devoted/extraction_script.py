#!/usr/bin/env python3
"""
Devoted Health Payment Policy Extraction Engine
================================================

Extracts complete, structured, and interpretable content from all Devoted Health
Medicare Advantage payment policy documents (PDFs), preserving narrative prose,
hierarchical sections, bulleted rules, code mentions, and rich tabular formats.

Outputs:
  1. Individual policy JSONs:
     devoted_health/extracted_contents_json/<policy_name>.json
  2. Consolidated master JSON:
     devoted_health/extraction_rules_devoted.json

Execution:
  python devoted_health/extraction_script.py
"""

import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pymupdf

# ============================================================
# CPT MAPPING ELIGIBILITY GATE
# ------------------------------------------------------------
# The KB mapping (cpt_mapping_bridge) must run for a policy ONLY when
#   1. it has no explicit CPT/HCPCS codes (own text, tables, linked PDFs),
#   2. it genuinely discusses procedures / services that carry codes, and
#   3. it is not an administrative / operational policy.
# The decision is taken here, per policy, BEFORE the bridge is called;
# the bridge only ever receives eligible policies (as deep copies) and
# its result is merged back without touching existing extracted data.
# ============================================================

import copy as _copy

KB_MARK = "KB PIPELINE MARKED"   # written on every policy whose codes came from the KB pipeline
import inspect as _inspect
import traceback as _traceback

CPT_CODE_RANGES = (
    (100, 1999), (10004, 69999), (70010, 79999), (80047, 89999), (90281, 99607),
)
_ALNUM_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-V]\d{4}|\d{4}[FT])(?![A-Za-z0-9])")
_NUM_CODE_RE = re.compile(r"(?<![\w$.,/-])(\d{5})(?![\w]|-\d{4}|\.\d)")
_CODE_CONTEXT_RE = re.compile(r"\b(?:cpt|hcpcs|codes?|procedures?|billed|reported|reporting)\b", re.I)
_STATE_BEFORE_RE = re.compile(r"\b[A-Z]{2}\s*$")

PROCEDURE_TERMS_RE = re.compile(
    r"\b(surg(?:ery|ical)|procedures?|biops(?:y|ies)|endoscop\w*|colonoscop\w*|injections?|infusions?|"
    r"imaging|radiolog\w*|x-rays?|mri|ct scan|ultrasound|anesthesia|laborator(?:y|ies)|assays?|"
    r"screening|therap(?:y|ies|eutic)|dialysis|transplant\w*|implant\w*|catheter\w*|diagnostic|"
    r"chemotherapy|radiation|vaccin\w*|immuniz\w*|rehabilitation|evaluation and management|"
    r"testing|excision|repair|reconstruct\w*|ablation|stent\w*|angiograph\w*|echocardiogra\w*|"
    r"electrocardiogra\w*|specimen|patholog\w*|supplies|equipment|device|visit|examination)\b", re.I)
ADMIN_TERMS_RE = re.compile(
    r"\b(disputes?|appeals?|grievances?|enrol+ment|credentialing|timely filing|claim submission|"
    r"clean claims?|coordination of benefits|third[- ]party liability|cost[- ]shar\w*|assignment|"
    r"attribution|notification|remittance|overpayments?|recoup\w*|audits?|network participation|"
    r"provider directory|eligibility|member id|nomnc|readmissions?)\b", re.I)
ADMIN_TITLE_RE = re.compile(
    r"dispute|appeal|grievance|enrol+ment|credential|timely filing|claims? (?:submission|reporting)|"
    r"reporting standards|coordination of benefits|cost[- ]shar|assignment|attribution|nomnc|"
    r"readmission|directory|eligibility|overpayment|notification|disclaimer", re.I)
MIN_PROCEDURE_TERMS = 4          # distinct procedural terms needed to call a policy procedural
MIN_NUMERIC_CODES = 3            # bare 5-digit numbers need this many (or a code keyword nearby)


# ------------------------------------------------------------
# LINE OF BUSINESS FILTER: only Medicare Advantage policies are extracted.
# Commercial, Medicaid and any other / unknown LOB are skipped.
# ------------------------------------------------------------
TARGET_LOB = "medicare advantage"
ALLOW_MIXED_LOB = True     # "Medicare Advantage and Commercial Products" still applies to MA members;
                           # set False to keep strictly MA-only policies
_OTHER_LOB_RE = re.compile(r"commercial|medicaid|marketplace|exchange|individual and family|dual", re.I)


def is_target_lob(lob):
    """True only for Medicare Advantage policies; None / unknown / other LOBs are skipped."""
    if not isinstance(lob, str) or TARGET_LOB not in lob.lower():
        return False
    if not ALLOW_MIXED_LOB and _OTHER_LOB_RE.search(lob.lower().replace(TARGET_LOB, "")):
        return False
    return True


def _flatten_strings(obj, out=None, limit=400000):
    """Collect every text value of a nested JSON-like object (urls excluded)."""
    if out is None:
        out = []
    if sum(len(x) for x in out) > limit:
        return out
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("url", "bbox", "raw_grid"):
                continue
            _flatten_strings(v, out, limit)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_strings(v, out, limit)
    return out


def _in_cpt_range(token):
    n = int(token)
    return any(lo <= n <= hi for lo, hi in CPT_CODE_RANGES)


def strict_explicit_codes(text, listed_codes=None):
    """Real CPT / HCPCS codes only.
    - HCPCS (A1234) and CPT category II/III (1234F / 1234T) always count;
    - bare 5-digit numbers must fall in a CPT range, must not look like a zip code
      and need a code keyword nearby, or at least MIN_NUMERIC_CODES distinct values."""
    text = text or ""
    found = set(_ALNUM_CODE_RE.findall(text))
    numeric, with_context = set(), set()
    for m in _NUM_CODE_RE.finditer(text):
        tok = m.group(1)
        if not _in_cpt_range(tok) or _STATE_BEFORE_RE.search(text[max(0, m.start() - 4):m.start()]):
            continue
        numeric.add(tok)
        window = text[max(0, m.start() - 80): m.end() + 80]
        if _CODE_CONTEXT_RE.search(window):
            with_context.add(tok)
    if len(numeric) >= MIN_NUMERIC_CODES:
        found |= numeric
    else:
        found |= with_context
    # codes the extractor listed (UHC / Humana CPT_CODES_USED): same validation, no blind trust
    listed = [str(c).strip().upper() for c in (listed_codes or []) if c]
    alnum = {c for c in listed if _ALNUM_CODE_RE.fullmatch(c)}
    nums = {c for c in listed if re.fullmatch(r"\d{5}", c) and _in_cpt_range(c)}
    found |= alnum
    if len(nums) >= MIN_NUMERIC_CODES:
        found |= nums
    return sorted(found)


def decide_cpt_mapping(titles, text, listed_codes=None, existing_mapping=False, linked_failed=None):
    """Return the per-policy mapping decision. eligible is True only for route MAP_NEEDED."""
    titles = [t for t in (titles or []) if isinstance(t, str) and t.strip()]
    text = text if isinstance(text, str) else ""
    explicit = strict_explicit_codes(text, listed_codes)
    proc_terms = sorted({m.lower() for m in PROCEDURE_TERMS_RE.findall(text)})
    admin_terms = sorted({m.lower() for m in ADMIN_TERMS_RE.findall(text)})
    title_admin = any(ADMIN_TITLE_RE.search(t) for t in titles)

    if explicit:
        route, reason = "HAS_CODES", f"{len(explicit)} explicit CPT/HCPCS code(s) already in the policy"
    elif linked_failed:
        route, reason = "LINKED_PDF_UNAVAILABLE", f"{len(linked_failed)} linked PDF(s) could not be read; cannot rule out codes"
    elif existing_mapping:
        route, reason = "ALREADY_MAPPED", "mapped codes from an earlier run are already stored"
    elif title_admin or len(admin_terms) >= len(proc_terms) + 2:
        route, reason = "ADMIN", "administrative / operational policy (title or content)"
    elif len(proc_terms) < MIN_PROCEDURE_TERMS:
        route, reason = "NO_PROCEDURES", f"only {len(proc_terms)} procedural term(s); policy does not discuss procedures"
    else:
        route, reason = "MAP_NEEDED", f"no explicit codes; {len(proc_terms)} procedural terms"
    return {
        "eligible": route == "MAP_NEEDED", "route": route, "reason": reason,
        "explicit_code_count": len(explicit),
        "procedure_terms": proc_terms[:12], "admin_terms": admin_terms[:8],
    }


def _policy_key(p):
    return (str(p.get("policy_number") or ""), str(p.get("policy_title") or ""), str(p.get("effective_date") or ""))


def _find_kb_root():
    """Folder holding scripts/map_policy_cpt_codes.py ('KB creation v1')."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for cand in (os.path.join(root, "KB creation v1"), os.path.join(here, "KB creation v1"),
                 os.path.join(os.path.dirname(root), "KB creation v1"), root):
        if os.path.isfile(os.path.join(cand, "scripts", "map_policy_cpt_codes.py")):
            return cand
    return None


def _kb_cli_integrate(policies, payer_profile=None, log=None):
    """Adapter for the KB pipeline's map_policy_cpt_codes.py (a command-line script).
    The eligible policies are written to KB/data/input/, the script is run on that file, and the
    resulting rows of policy_cpt_mappings are read back from the KB DuckDB and attached to each policy
    as mapped_cpt_codes."""
    import subprocess as _sp
    kb = _find_kb_root()
    if not kb:
        raise RuntimeError("KB creation v1/scripts/map_policy_cpt_codes.py not found")
    in_dir = os.path.join(kb, "data", "input")
    os.makedirs(in_dir, exist_ok=True)
    in_file = os.path.join(in_dir, f"{payer_profile}_auto_mapping_input.json")
    with open(in_file, "w", encoding="utf-8") as fh:
        json.dump(policies, fh, indent=2, ensure_ascii=False)
    cmd = [sys.executable, os.path.join(kb, "scripts", "map_policy_cpt_codes.py"),
           "--input", in_file, "--payer-profile", str(payer_profile)]
    proc = _sp.run(cmd, cwd=kb, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"map_policy_cpt_codes.py exited {proc.returncode}: {(proc.stderr or proc.stdout)[-1500:]}")
    import duckdb
    db_path = os.path.join(kb, "database", "payer_policy_kb.duckdb")
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT p.policy_title, m.code_system, m.code, m.confidence_tier, m.score "
            "FROM policy_cpt_mappings m JOIN policies p ON p.policy_uid = m.policy_uid").fetchall()
    finally:
        con.close()
    by_title = {}
    for title, system, code, tier, score in rows:
        by_title.setdefault(str(title).strip().lower(), []).append(
            {"code": code, "code_system": system or "CPT/HCPCS", "description": "",
             "confidence_tier": tier, "score": score,
             "mapping_origin": "inferred", "best_method": "kb_pipeline"})
    mapped = 0
    for p in policies:
        hits = by_title.get(str(p.get("policy_title") or "").strip().lower(), [])
        if hits:
            p["mapped_cpt_codes"] = hits
            mapped += 1
    return policies, mapped


def _load_mapping_function():
    """Locate the pipeline's CPT mapping entry point.
    Looks in cpt_mapping_bridge.py and map_policy_cpt_codes.py, searching the project root
    and 'KB creation v1/scripts' (the folder this script's client folder sits in)."""
    import importlib
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for extra in (root, os.path.join(root, "KB creation v1", "scripts"),
                  os.path.join(root, "KB creation v1"), os.path.dirname(root)):
        if os.path.isdir(extra) and extra not in sys.path:
            sys.path.insert(0, extra)
    errors = []
    for modname in ("cpt_mapping_bridge", "map_policy_cpt_codes"):
        try:
            mod = importlib.import_module(modname)
        except Exception as exc:
            errors.append(f"{modname}: {type(exc).__name__}: {exc}")
            continue
        fn = getattr(mod, "integrate_cpt_mapping", None)
        if callable(fn):
            return fn
        errors.append(f"{modname}: no integrate_cpt_mapping function")
    if _find_kb_root():
        return _kb_cli_integrate
    raise ImportError("; ".join(errors))


def apply_cpt_mapping(policies, payer_profile, log=print):
    """Run the KB bridge ONLY on policies whose p["mapping_eligibility"]["eligible"] is True.
    Results are merged back into the original policy objects without overwriting anything:
    explicit codes and all other fields stay as extracted, mapped codes go to mapped_cpt_codes."""
    def say(msg):
        try:
            (log.info if hasattr(log, "info") else log)(msg)
        except Exception:
            print(msg)

    todo = [i for i, p in enumerate(policies)
            if isinstance(p, dict) and (p.get("mapping_eligibility") or {}).get("eligible")]
    for p in policies:
        if isinstance(p, dict):
            route = (p.get("mapping_eligibility") or {}).get("route", "UNCHECKED")
            p.setdefault("mapping_status", f"skipped:{route}" if route != "MAP_NEEDED" else "pending")
    say(f"CPT mapping gate [{payer_profile}]: {len(todo)} of {len(policies)} policies eligible")
    if not todo:
        return policies, 0

    try:
        integrate_cpt_mapping = _load_mapping_function()
    except Exception as exc:
        say("KB mapping skipped (knowledge base not connected); eligible policies left unmapped")
        for i in todo:
            policies[i]["mapping_status"] = "bridge_unavailable"
        return policies, 0

    batch = _copy.deepcopy([policies[i] for i in todo])      # the bridge can never half-edit the originals
    kwargs = {"payer_profile": payer_profile}
    try:
        if "log" in _inspect.signature(integrate_cpt_mapping).parameters and hasattr(log, "info"):
            kwargs["log"] = log
        returned, _ = integrate_cpt_mapping(batch, **kwargs)
    except Exception as exc:
        say(f"CPT mapping failed for {payer_profile}: {type(exc).__name__}: {exc}\n{_traceback.format_exc()}")
        for i in todo:
            policies[i]["mapping_status"] = "mapping_error"
        return policies, 0

    by_key = {_policy_key(r): r for r in (returned or []) if isinstance(r, dict)}
    mapped_count = 0
    for pos, i in enumerate(todo):
        orig = policies[i]
        new = by_key.get(_policy_key(orig)) or (returned[pos] if isinstance(returned, list) and len(returned) == len(todo)
                                                and isinstance(returned[pos], dict) else None)
        if new is None:
            orig["mapping_status"] = "no_result"
            continue
        have = {str(m.get("code")) for m in (orig.get("mapped_cpt_codes") or []) if isinstance(m, dict)}
        add = []
        for m in (new.get("mapped_cpt_codes") or []):
            if isinstance(m, dict) and m.get("code") and str(m["code"]) not in have:
                have.add(str(m["code"]))
                add.append(m)
        explicit_before = set(orig.get("CPT_CODES_USED") or [])
        for code in (new.get("CPT_CODES_USED") or []):          # a bridge that wrote into CPT_CODES_USED
            if code not in explicit_before and str(code) not in have:
                have.add(str(code))
                add.append({"code": code, "code_system": "CPT/HCPCS", "description": "",
                            "mapping_origin": "inferred", "best_method": "bridge_cpt_codes_used"})
        if add:
            for m in add:
                m["kb_marker"] = KB_MARK
            orig["mapped_cpt_codes"] = list(orig.get("mapped_cpt_codes") or []) + add
            orig["mapping_source"] = KB_MARK
            orig["kb_pipeline_marked"] = True
            orig["mapping_status"] = "mapped"
            mapped_count += 1
        else:
            orig["mapping_status"] = "none_found"
        for k, v in new.items():                                 # keep any extra bridge keys, never overwrite
            if k not in orig and k not in ("CPT_CODES_USED", "mapped_cpt_codes"):
                orig[k] = v
    say(f"CPT mapping: {mapped_count} of {len(todo)} eligible policies received mapped codes")
    return policies, mapped_count


def _devoted_prepare_mapping(policies):
    """Annotate each Devoted policy with its mapping decision."""
    for p in policies:
        listed = (p.get("codes_found") or {}).get("cpt_hcpcs") or []
        p.setdefault("CPT_CODES_USED", list(listed))
        prior = any(isinstance(m, dict) for m in (p.get("mapped_cpt_codes") or []))
        text = " ".join(_flatten_strings(p.get("extracted_content")) + _flatten_strings(p.get("tables")))
        p["mapping_eligibility"] = decide_cpt_mapping(
            [p.get("policy_title")], text, p.get("CPT_CODES_USED"), existing_mapping=prior)
        if prior:
            p["mapping_status"] = "mapped"
    return policies

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("devoted_extractor")

# ---------------------------------------------------------------------------
# Constants & Directory Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
POLICIES_DIR = BASE_DIR / "policies"
OUTPUT_DIR = BASE_DIR / "extracted_contents_json"
MASTER_JSON_PATH = BASE_DIR / "extraction_rules_devoted.json"

# Known top-level section keywords in Devoted Health policies
KNOWN_TOP_SECTIONS = [
    "PURPOSE", "OVERVIEW", "SCOPE", "APPLICABILITY", "POLICY",
    "PAYMENT POLICY", "PAYMENT POLICY GUIDELINES", "POLICY STATEMENT",
    "COVERAGE GUIDELINES", "REIMBURSEMENT GUIDELINES", "HOW TO BILL YOUR CLAIMS",
    "BILLING REQUIREMENTS", "BILLING GUIDELINES", "BILLING INSTRUCTIONS",
    "WHAT WE COVER", "INCOMPLETE TREATMENT",
    "ACRONYMS AND DEFINITIONS", "DEFINITIONS", "RESPONSIBILITIES",
    "CODING GUIDELINES", "CODING REQUIREMENTS", "EXCLUDED SERVICES",
    "NON-COVERED SERVICES", "REGULATORY REFERENCE", "REGULATORY REFERENCES",
    "REFERENCES", "RELATED POLICIES, DOCUMENTS, AND CONTROLS",
    "RELATED POLICIES", "REVIEW AND VERSION HISTORY", "REVIEW",
    "POLICY APPROVAL AND HISTORY", "DISCLAIMER"
]

# Standard EDI Loops and non-procedure numbers to exclude from CPT/HCPCS
EXCLUDED_CPT_CODES = {
    "2000A", "2010A", "2010AA", "2010AB", "2010BA", "2010BB",
    "2300", "2310A", "2310B", "2310C", "2310D", "2310E", "2310F",
    "2400", "2410", "2420A", "2420B", "2420C", "2420D", "2420E", "2420F", "2420G", "2420H",
    "508", "032026", "0508", "0509"
}

# Known medical modifiers
KNOWN_MODIFIERS = {
    "22", "24", "25", "26", "50", "51", "52", "53", "57", "58", "59",
    "62", "66", "76", "77", "78", "79", "80", "81", "82", "90", "91", "92", "93", "95",
    "AA", "AD", "AH", "AJ", "AM", "AQ", "AR", "AS", "AT", "BO", "CA", "CB", "CC", "CD",
    "CE", "CF", "CR", "CS", "ET", "EY", "FB", "FC", "FP", "FQ", "FR", "FX", "FY",
    "G0", "G8", "G9", "GA", "GC", "GE", "GG", "GH", "GJ", "GK", "GL", "GM", "GN", "GO",
    "GP", "GQ", "GR", "GS", "GT", "GV", "GW", "GX", "GY", "GZ",
    "HA", "HB", "HC", "HD", "HE", "HF", "HG", "HH", "HI", "HJ", "HK", "HL", "HM", "HN",
    "HO", "HP", "HQ", "HR", "HS", "HT", "HU", "HV", "HW", "HX", "HY", "HZ",
    "JA", "JB", "JC", "JD", "JE", "JF", "JG", "JK", "JL", "JW", "JZ", "KC", "KD", "KF", "KM", "KN",
    "KO", "KP", "KQ", "KR", "KS", "KT", "KX", "KZ", "LC", "LD", "LL", "LM", "LR", "LS",
    "LT", "MS", "NR", "NU", "PA", "PB", "PC", "PD", "PI", "PL", "PM", "PN", "PO", "PS",
    "PT", "Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "QA", "QB", "QC",
    "QD", "QE", "QF", "QG", "QH", "QJ", "QK", "QL", "QM", "QN", "QP", "QQ", "QS", "QT",
    "QU", "QV", "QW", "QX", "QY", "QZ", "RA", "RB", "RC", "RD", "RE", "RI", "RR", "RT",
    "SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SJ", "SL", "SM", "SN", "TC", "TD",
    "TE", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TP", "TQ", "TR", "TS", "TT",
    "TU", "TV", "TW", "U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9", "UA", "UB",
    "UC", "UD", "UE", "UF", "UG", "UH", "UJ", "UK", "UN", "UP", "UQ", "UR", "US", "VM",
    "VP", "VR", "XE", "XP", "XS", "XU"
}

# Canonical policy number mappings for policies without an explicit [CLMS.CP.xxxx] header
CANONICAL_POLICY_NUMBERS = {
    "Acute_Admissions_and_Level_of_Care.pdf": "DH-PP-ACUTE-25",
    "Acute_Hospital_Care_at_Home.pdf": "DH-PP-ACHAH",
    "Billing_and_Cost_Sharing_for_Dual_Eligible_Members.pdf": "DH-PP-DUAL-2025",
    "Claims_based_PCP_Assignment_Policy.pdf": "3726DHPV5",
    "Critical_Access_Hospital_CAH_Billing.pdf": "DH-PP-CAH-2024",
    "Dental_Crowns.pdf": "DH-PP-DENT-030326",
    "Emergency_Department_Services.pdf": "EP30",
    "Home_Health_Services_Billing.pdf": "DH-PP-HHA",
    "Hospital_Readmissions.pdf": "DH-PP-READM-2025",
    "NOMNC_Compliance_Documentation_and_Provider_Requirements_in_Appeals.pdf": "DH-PP-NOMNC-2025",
    "Observation_Services.pdf": "EPP-03",
    "Organ_Transplants.pdf": "EPP-23",
    "Preventive_Services.pdf": "EP-PREVENTIVE",
    "Primary_Care_Services.pdf": "EPP-PCS",
}


# ---------------------------------------------------------------------------
# Text Normalization & Code Extraction Helpers
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """
    Normalize text:
    - CRITICAL FIX: Replaces zero-width spaces (\u200b, \u200c, \u200d, \ufeff) with
      a regular ASCII space so that documents using \u200b as word separators
      (such as Dental Crowns and Home Health Services) do not fuse words together.
    - Normalizes unicode dashes, non-breaking spaces, and quotes.
    - Collapses multiple whitespace.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    # Replace zero-width spaces with regular space to prevent word fusion
    t = re.sub(r"[\u200b\u200c\u200d\ufeff]+", " ", t)
    t = t.replace("\xa0", " ")
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    t = t.replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    # Collapse multiple whitespace within line
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def extract_codes(text: str) -> Dict[str, List[str]]:
    """
    Extract and categorize medical codes mentioned within the text:
    - CPT / HCPCS procedure codes
    - Medical Modifiers
    - UB Revenue Codes (3-4 digits)
    - UB Bill Types (e.g. 0831-0839, 0711)
    - ICD-10 Diagnosis Codes
    """
    results: Dict[str, Set[str]] = {
        "cpt_hcpcs": set(),
        "modifiers": set(),
        "revenue_codes": set(),
        "bill_types": set(),
        "icd_codes": set(),
    }
    if not text:
        return {k: [] for k in results}

    norm = normalize_text(text)

    # 1. CPT / HCPCS codes & code ranges
    ranges = re.findall(r"\b([0-9]{5})\s*(?:to|through|–|-)\s*([0-9]{5})\b", norm, re.I)
    for start_c, end_c in ranges:
        if start_c.isdigit() and end_c.isdigit():
            s_val, e_val = int(start_c), int(end_c)
            if 0 < e_val - s_val <= 30:
                for c_val in range(s_val, e_val + 1):
                    results["cpt_hcpcs"].add(f"{c_val:05d}")

    cpt_matches = re.findall(r"\b([0-9]{5}|[A-Z][0-9]{4})\b", norm)
    for c in cpt_matches:
        if c in EXCLUDED_CPT_CODES:
            continue
        # Filter years (1990-2035) and tiny numbers
        if c.isdigit():
            c_int = int(c)
            if 1990 <= c_int <= 2035 or c_int < 100:
                continue
        results["cpt_hcpcs"].add(c)

    # 2. Modifiers
    mod_explicit = re.findall(
        r"\b(?:modifier[s]?\s+|mod[s]?\s+)([A-Z0-9]{2})\b", norm, re.I
    )
    for m in mod_explicit:
        m_up = m.upper()
        if m_up in KNOWN_MODIFIERS:
            results["modifiers"].add(m_up)

    # Leading modifier pattern: e.g. "52  Reduced services", "AS  Physician assistant"
    leading_mod = re.match(r"^([0-9]{2}|[A-Z]{2})\s+([A-Z].*)", norm)
    if leading_mod:
        cand_mod = leading_mod.group(1).upper()
        if cand_mod in KNOWN_MODIFIERS:
            results["modifiers"].add(cand_mod)

    # Standalone modifiers if following known prefixes
    for km in ("KX", "GY", "JW", "JZ", "Q6", "25", "59", "95", "XE", "XP", "XS", "XU", "NU", "RR"):
        if re.search(rf"\b{km}\b", norm):
            results["modifiers"].add(km)

    # 3. Revenue codes
    rev_explicit = re.findall(
        r"\b(?:revenue code[s]?\s+|rev code[s]?\s+)([0-9]{3,4})\b", norm, re.I
    )
    for r in rev_explicit:
        results["revenue_codes"].add(r.zfill(4))

    # Revenue code patterns like 0490, 0521, 0360, 0762 when in billing context
    if any(k in norm.lower() for k in ("revenue", "rev code", "ub-04", "ub04", "fl 42")):
        rev_generic = re.findall(r"\b(0[1-9][0-9]{2})\b", norm)
        for r in rev_generic:
            results["revenue_codes"].add(r)

    # 4. Bill types
    bill_matches = re.findall(
        r"\b(?:bill type[s]?\s+|type of bill[s]?\s+)([0-9]{3,4}(?:[–\-][0-9]{3,4})?)\b",
        norm,
        re.I,
    )
    for b in bill_matches:
        results["bill_types"].add(b)

    # 5. ICD-10 diagnosis codes
    icd_matches = re.findall(
        r"\b([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b", norm
    )
    for icd in icd_matches:
        if "." in icd and not icd.split(".")[0].isalnum():
            continue
        results["icd_codes"].add(icd)

    return {k: sorted(list(v)) for k, v in results.items()}


# ---------------------------------------------------------------------------
# Section Heading Detection Helper
# ---------------------------------------------------------------------------
def classify_heading(text: str) -> Tuple[bool, bool, str]:
    """
    Classify whether a line is a section heading or subsection heading.
    Returns: (is_heading, is_top_section, clean_title)
    """
    clean = normalize_text(text)
    if not clean or len(clean) > 90:
        return False, False, clean

    # Headings do NOT end with full stops, semicolons, or commas
    if clean.endswith((".", ";", ",")):
        return False, False, clean

    # Bullets are not headings
    if clean.startswith(("●", "•", "-", "○", "▪", "*")):
        return False, False, clean

    clean_up = clean.upper()

    # Exact match for top-level section keywords
    for sec in KNOWN_TOP_SECTIONS:
        if clean_up == sec or clean_up.startswith(sec + ":"):
            return True, True, sec

    # All uppercase short title
    if clean.isupper() and 3 <= len(clean) <= 60 and not re.search(r"\b(PAGE|DATE|FORM|LOOP)\b", clean_up):
        return True, True, clean

    # Subsection headings: e.g. "How to bill your claims", "Facility Charges", "Key Notes"
    if not clean.endswith(":") and len(clean.split()) <= 8 and (
        any(k in clean.lower() for k in (
            "billing", "facility charges", "professional charges", "encounter",
            "criteria", "payment", "requirements", "guidelines", "instructions",
            "telehealth", "modifiers", "definitions", "rates", "prior authorization",
            "frequency", "presumption", "inpatient", "outpatient", "observation"
        )) or clean.istitle()
    ):
        return True, False, clean

    return False, False, clean


# ---------------------------------------------------------------------------
# Table Extraction & Structuring
# ---------------------------------------------------------------------------
def extract_page_tables(page, page_num: int, prev_table_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Extract all tables from a page with structured column names, row dictionaries,
    markdown rendering, cell-level code extraction, and multi-page continuation support.
    """
    extracted_tables: List[Dict[str, Any]] = []
    try:
        tables = page.find_tables().tables
    except Exception as e:
        logger.debug("Table detection error on page %d: %s", page_num, e)
        return []

    for idx, tab in enumerate(tables, 1):
        raw_rows = tab.extract()
        if not raw_rows or len(raw_rows) < 1:
            continue

        # Clean row cells with normalize_text (replaces \u200b with spaces)
        cleaned_grid: List[List[str]] = []
        for r in raw_rows:
            cleaned_row = [normalize_text(str(c or "")) for c in r]
            if any(cleaned_row):
                cleaned_grid.append(cleaned_row)

        if not cleaned_grid:
            continue

        first_row = cleaned_grid[0]
        non_empty_first = [c for c in first_row if c]
        table_title: Optional[str] = None
        data_rows_start = 1

        # Check if this table is a multi-page continuation from the previous page
        is_continuation = False
        inherited_columns: Optional[List[str]] = None
        if prev_table_context and prev_table_context.get("col_count") == len(first_row):
            first_val = first_row[0]
            # If first cell is empty, a code, or not a capitalized header label:
            if not first_val or first_val.isdigit() or first_val.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "G", "●")) or not first_val.isupper():
                is_continuation = True
                inherited_columns = prev_table_context.get("columns")
                table_title = f"{prev_table_context.get('title')} (Continued)"
                data_rows_start = 0  # Row 0 is data, not header!

        if not is_continuation:
            # Check if first row is a single spanning table title banner
            if len(non_empty_first) == 1 and len(first_row) > 1:
                table_title = non_empty_first[0]
                if len(cleaned_grid) > 1:
                    raw_cols = cleaned_grid[1]
                    data_rows_start = 2
                else:
                    raw_cols = [table_title]
                    data_rows_start = 1
            else:
                raw_cols = first_row
                data_rows_start = 1

            # Handle spanning group headers with forward fill:
            # E.g. ['Form Type', '837I (Electronic)', '', 'UB-04 (Paper)', '']
            filled_cols: List[str] = []
            curr_group = ""
            for c_idx, c_val in enumerate(raw_cols):
                if c_val:
                    curr_group = c_val
                    filled_cols.append(c_val)
                elif curr_group:
                    filled_cols.append(f"{curr_group} (Detail)")
                elif c_idx == 0:
                    # Top-left matrix cell (e.g. Paper / Electronic)
                    filled_cols.append("Submission Format")
                else:
                    filled_cols.append(f"Column_{c_idx + 1}")

            # Normalize column names: ensure uniqueness
            col_names: List[str] = []
            seen_cols: Dict[str, int] = {}
            for col in filled_cols:
                if col in seen_cols:
                    seen_cols[col] += 1
                    col_names.append(f"{col}_{seen_cols[col]}")
                else:
                    seen_cols[col] = 1
                    col_names.append(col)
        else:
            col_names = inherited_columns or [f"Column_{i+1}" for i in range(len(first_row))]

        # Build structured row records
        records: List[Dict[str, str]] = []
        table_text_accumulator: List[str] = []
        for r in cleaned_grid[data_rows_start:]:
            if not any(r):
                continue
            row_dict: Dict[str, str] = {}
            for col_i, col_name in enumerate(col_names):
                val = r[col_i] if col_i < len(r) else ""
                row_dict[col_name] = val
                if val:
                    table_text_accumulator.append(val)
            records.append(row_dict)

        # Generate readable Markdown representation
        md_lines = [
            "| " + " | ".join(col_names) + " |",
            "| " + " | ".join(["---"] * len(col_names)) + " |",
        ]
        for rd in records:
            md_lines.append("| " + " | ".join(rd.get(c, "") for c in col_names) + " |")
        md_table = "\n".join(md_lines)

        # Extract codes across all cells in table
        combined_table_text = " ".join(table_text_accumulator)
        codes = extract_codes(combined_table_text)

        extracted_tables.append({
            "table_id": f"tab_p{page_num}_{idx}",
            "title": table_title or f"Table (Page {page_num})",
            "page_number": page_num,
            "bbox": list(tab.bbox),
            "columns": col_names,
            "row_count": len(records),
            "col_count": len(col_names),
            "rows": records,
            "raw_grid": cleaned_grid,
            "markdown": md_table,
            "codes": codes,
        })

    return extracted_tables


# ---------------------------------------------------------------------------
# Metadata Extractor
# ---------------------------------------------------------------------------
def extract_policy_metadata(
    doc,
    filename: str,
    url_map: Dict[str, str],
) -> Dict[str, Any]:
    """
    Extract comprehensive document metadata:
    - Policy Title
    - Policy Number (canonical standard identifiers)
    - Effective Date / Publication Date
    - Version
    - Line of Business (Medicare Advantage)
    - Billing Class (Professional / Institutional / Both)
    - Revision History list
    """
    p0_text = normalize_text(doc[0].get_text() if len(doc) > 0 else "")
    p_last_text = normalize_text(doc[-1].get_text() if len(doc) > 0 else "")

    # 1. Policy Number
    pol_num: Optional[str] = None
    m_num = re.search(r"\[?(CLMS\.CP\.\d{4})\]?", p0_text)
    if m_num:
        pol_num = m_num.group(1)
    else:
        # Check last page revision table for document #
        m_rev_num = re.search(r"(CLMS\.CP\.\d{4})", p_last_text)
        if m_rev_num:
            pol_num = m_rev_num.group(1)
        elif filename in CANONICAL_POLICY_NUMBERS:
            pol_num = CANONICAL_POLICY_NUMBERS[filename]
        else:
            orig_url = url_map.get(filename, "")
            m_url = re.search(r"(CLMS\.CP\.\d{4}|EPP[_\-]\d+|PP[_\-]\d+|DH[_\-]P[_\-]\d+|EP30)", orig_url)
            if m_url:
                pol_num = m_url.group(1).replace("_", "-")
            else:
                pol_num = f"DEV-{Path(filename).stem[:20]}"

    # 2. Effective Date
    eff_date: Optional[str] = None
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})", p0_text)
    if m_date:
        eff_date = m_date.group(1)
    else:
        m_rev_date = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", p_last_text)
        if m_rev_date:
            eff_date = m_rev_date.group(1)
        else:
            eff_date = "2026-01-01"

    # 3. Version
    version_val = "1.0"
    m_ver = re.search(r"\bv(\d+\.\d+)\b", p0_text)
    if m_ver:
        version_val = m_ver.group(1)
    else:
        m_rev_ver = re.search(r"Revision\s*#?\s*\n*(\d+(?:\.\d+)?)", p_last_text, re.I)
        if m_rev_ver:
            version_val = m_rev_ver.group(1)

    # 4. Revision History Table from last page
    revision_history: List[Dict[str, str]] = []
    try:
        last_page_tabs = doc[-1].find_tables().tables
        for t in last_page_tabs:
            ext = t.extract()
            if ext and len(ext) > 1 and any("date" in str(c).lower() for c in ext[0]):
                headers = [normalize_text(str(c or "")).lower().replace(" ", "_").replace("#", "num") for c in ext[0]]
                for r in ext[1:]:
                    clean_r = [normalize_text(str(c or "")) for c in r]
                    if any(clean_r):
                        revision_history.append(dict(zip(headers, clean_r)))
    except Exception as e:
        logger.debug("Revision table parse error: %s", e)

    # 5. Billing Class Inference
    full_doc_text = " ".join(normalize_text(p.get_text()) for p in doc)
    has_prof = any(k in full_doc_text.lower() for k in ("cms-1500", "837p", "professional claims", "physician charges"))
    has_inst = any(k in full_doc_text.lower() for k in ("ub-04", "ub04", "837i", "institutional claims", "facility charges"))
    if has_prof and has_inst:
        billing_class = "Both"
    elif has_inst:
        billing_class = "Institutional"
    elif has_prof:
        billing_class = "Professional"
    else:
        billing_class = "Both"

    # 6. Policy Title from clean filename
    policy_title = Path(filename).stem.replace("_", " ")

    return {
        "policy_title": policy_title,
        "policy_number": pol_num,
        "effective_date": eff_date,
        "version": version_val,
        "source_pdf": filename,
        "source_url": url_map.get(filename, ""),
        "line_of_business": "Medicare Advantage",
        "billing_class": billing_class,
        "page_count": len(doc),
        "revision_history": revision_history,
    }


# ---------------------------------------------------------------------------
# Core Policy Content Extraction
# ---------------------------------------------------------------------------
def extract_policy_document(
    pdf_path: Path,
    url_map: Dict[str, str],
) -> Dict[str, Any]:
    """
    Extract all narrative content, structured tables, and categorized codes
    for a single policy PDF, preserving exact hierarchical order and table footnotes.
    """
    doc = pymupdf.open(pdf_path)
    metadata = extract_policy_metadata(doc, pdf_path.name, url_map)

    current_section = "OVERVIEW"
    current_subsection: Optional[str] = None
    all_sections_seen: List[str] = ["OVERVIEW"]

    extracted_content: List[Dict[str, Any]] = []
    all_document_tables: List[Dict[str, Any]] = []
    policy_codes_aggregate: Dict[str, Set[str]] = {
        "cpt_hcpcs": set(),
        "modifiers": set(),
        "revenue_codes": set(),
        "bill_types": set(),
        "icd_codes": set(),
    }

    item_counter = 1
    last_table_context: Optional[Dict[str, Any]] = None

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # 1. Extract tables on this page (with continuation awareness)
        page_tables = extract_page_tables(page, page_num, prev_table_context=last_table_context)
        table_bboxes = [t["bbox"] for t in page_tables]
        if page_tables:
            last_table_context = page_tables[-1]

        # Gather all cell text for tables on this page
        page_table_cells: Set[str] = set()
        for pt in page_tables:
            for r in pt["raw_grid"]:
                for c in r:
                    if c:
                        for l in c.splitlines():
                            page_table_cells.add(normalize_text(l).lower())

        # 2. Extract text blocks
        blocks = page.get_text("blocks")
        # Sort vertically by top y-coordinate, then left x-coordinate
        blocks = sorted(blocks, key=lambda b: (b[1], b[0]))

        # Filter out blocks inside tables, headers, footers, and metadata watermarks
        text_elements: List[Tuple[str, List[float], str]] = []
        for b in blocks:
            bbox = list(b[:4])
            raw_txt = normalize_text(b[4])
            if not raw_txt:
                continue

            # Header bounds check (y < 45)
            if bbox[1] < 45:
                continue
            # Footer bounds check (y > 755) unless substantive text
            if bbox[3] > 755 and (
                "DEVOTED HEALTH" in raw_txt or "PUBLIC USE" in raw_txt or
                re.search(r"^CLMS\.CP\.\d{4}.*Page \d+ of \d+", raw_txt, re.I) or
                re.search(r"^\d+\s*\|\s*Payment Policy", raw_txt) or
                re.search(r"^Page \d+ of \d+$", raw_txt, re.I)
            ):
                continue

            # Check if block overlaps with any table bounding box
            in_table = False
            for tb in table_bboxes:
                if not (bbox[2] < tb[0] or bbox[0] > tb[2] or bbox[3] < tb[1] or bbox[1] > tb[3]):
                    in_table = True
                    break

            if in_table:
                # Check for footnote or extra lines in overlapping block that aren't table cells!
                lines = [normalize_text(l) for l in raw_txt.splitlines() if normalize_text(l)]
                extra_lines = [
                    l for l in lines
                    if l.lower() not in page_table_cells and
                    "DEVOTED HEALTH" not in l and "PUBLIC USE" not in l and
                    not re.search(r"^CLMS\.CP\.\d{4}", l) and
                    not re.search(r"^\d+\s*\|\s*Payment Policy", l)
                ]
                if extra_lines:
                    # Table footnote / explanatory note
                    footnote_txt = "\n".join(extra_lines)
                    text_elements.append(("text", [bbox[0], tb[3] + 1, bbox[2], bbox[3]], footnote_txt))
            else:
                text_elements.append(("text", bbox, raw_txt))

        # 3. Interleave text and tables by vertical y-coordinate
        page_elements: List[Tuple[str, List[float], Any]] = []
        for te in text_elements:
            page_elements.append(te)
        for pt in page_tables:
            page_elements.append(("table", pt["bbox"], pt))

        page_elements = sorted(page_elements, key=lambda el: el[1][1])

        # 4. Process elements in sequential flow
        for el_type, bbox, val in page_elements:
            if el_type == "table":
                tab_data = val
                all_document_tables.append(tab_data)

                # Merge table codes into policy aggregate
                for code_k, code_list in tab_data["codes"].items():
                    policy_codes_aggregate[code_k].update(code_list)

                item_codes = sorted(list(set(
                    c for sublist in tab_data["codes"].values() for c in sublist
                )))

                extracted_content.append({
                    "id": f"item_{item_counter:04d}",
                    "title": tab_data["title"],
                    "section": current_section,
                    "subsection": current_subsection,
                    "hierarchy_path": [p for p in [current_section, current_subsection] if p],
                    "page_number": page_num,
                    "page_start": page_num,
                    "page_end": page_num,
                    "content_type": "table",
                    "content": tab_data["markdown"],
                    "codes": item_codes,
                    "table_data": {
                        "table_id": tab_data["table_id"],
                        "title": tab_data["title"],
                        "columns": tab_data["columns"],
                        "row_count": tab_data["row_count"],
                        "rows": tab_data["rows"],
                    },
                })
                item_counter += 1

            else:
                # Text element
                raw_txt = val
                lines = [normalize_text(l) for l in raw_txt.splitlines() if normalize_text(l)]
                if not lines:
                    continue

                first_line = lines[0]
                is_heading, is_top, clean_heading = classify_heading(first_line)

                if is_heading and len(lines) == 1:
                    if is_top:
                        current_section = clean_heading
                        current_subsection = None
                        if clean_heading not in all_sections_seen:
                            all_sections_seen.append(clean_heading)
                    else:
                        current_subsection = clean_heading

                    codes_in_h = extract_codes(clean_heading)
                    flat_h_codes = sorted(list(set(
                        c for sublist in codes_in_h.values() for c in sublist
                    )))
                    for ck, cv in codes_in_h.items():
                        policy_codes_aggregate[ck].update(cv)

                    extracted_content.append({
                        "id": f"item_{item_counter:04d}",
                        "title": clean_heading,
                        "section": current_section,
                        "subsection": current_subsection,
                        "hierarchy_path": [p for p in [current_section, current_subsection] if p],
                        "page_number": page_num,
                        "page_start": page_num,
                        "page_end": page_num,
                        "content_type": "header",
                        "content": clean_heading,
                        "codes": flat_h_codes,
                        "table_data": None,
                    })
                    item_counter += 1

                else:
                    # Paragraph, definition, footnote, or bullet points
                    for l in lines:
                        if not l:
                            continue

                        codes_in_line = extract_codes(l)
                        for ck, cv in codes_in_line.items():
                            policy_codes_aggregate[ck].update(cv)
                        flat_line_codes = sorted(list(set(
                            c for sublist in codes_in_line.values() for c in sublist
                        )))

                        # Determine content type
                        if l.startswith(("●", "•", "-", "○", "▪", "*")) or re.match(r"^\d+\.\s+", l):
                            ctype = "bullet_point"
                        elif ":" in l and len(l.split(":")[0].split()) <= 4 and len(l) > len(l.split(":")[0]) + 5:
                            ctype = "definition"
                        else:
                            ctype = "paragraph"

                        extracted_content.append({
                            "id": f"item_{item_counter:04d}",
                            "title": current_subsection or current_section,
                            "section": current_section,
                            "subsection": current_subsection,
                            "hierarchy_path": [p for p in [current_section, current_subsection] if p],
                            "page_number": page_num,
                            "page_start": page_num,
                            "page_end": page_num,
                            "content_type": ctype,
                            "content": l,
                            "codes": flat_line_codes,
                            "table_data": None,
                        })
                        item_counter += 1

    # Assemble complete policy dictionary
    result = dict(metadata)
    result["table_count"] = len(all_document_tables)
    result["codes_found"] = {k: sorted(list(v)) for k, v in policy_codes_aggregate.items()}
    result["CPT_CODES_USED"] = list(result["codes_found"].get("cpt_hcpcs", []))
    result["sections_summary"] = all_sections_seen
    result["tables"] = all_document_tables
    result["extracted_content"] = extracted_content

    return result


# ---------------------------------------------------------------------------
# Batch Processing & Output Generation
# ---------------------------------------------------------------------------
def process_all_policies(
    policies_dir: Path = POLICIES_DIR,
    output_dir: Path = OUTPUT_DIR,
    master_json_path: Path = MASTER_JSON_PATH,
) -> List[Dict[str, Any]]:
    """
    Extract content from all policies in policies_dir, write individual JSONs,
    and save the consolidated master JSON.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_files = sorted(list(policies_dir.glob("*.pdf")))

    logger.info("=" * 80)
    logger.info("DEVOTED HEALTH PAYMENT POLICY EXTRACTION ENGINE")
    logger.info("Input PDF Directory:  %s", policies_dir)
    logger.info("Output JSON Folder:   %s", output_dir)
    logger.info("Master Output JSON:   %s", master_json_path)
    logger.info("Found %d PDF policy files to extract.", len(pdf_files))
    logger.info("=" * 80)

    # Load original URL mappings from scraper
    url_map: Dict[str, str] = {}
    try:
        sys.path.insert(0, str(BASE_DIR))
        import scraper
        scraped_policies = scraper.scrape_policies(output_dir=policies_dir)
        url_map = {p["filename"]: p["original_url"] for p in scraped_policies}
    except Exception as e:
        logger.warning("Could not load dynamic URL map from scraper: %s", e)

    all_extracted_policies: List[Dict[str, Any]] = []
    total_items = 0
    total_tables = 0
    total_codes = 0

    for idx, pdf_path in enumerate(pdf_files, 1):
        logger.info("[%d/%d] Extracting: %s...", idx, len(pdf_files), pdf_path.name)
        policy_data = extract_policy_document(pdf_path, url_map)
        if not is_target_lob(policy_data.get("line_of_business")):
            logger.info("       -> SKIPPED (not Medicare Advantage): %s", pdf_path.name)
            continue

        # Write individual JSON
        stem = pdf_path.stem
        single_json_path = output_dir / f"{stem}.json"
        single_json_path.write_text(json.dumps(policy_data, indent=2, ensure_ascii=False), encoding="utf-8")

        all_extracted_policies.append(policy_data)

        # Metrics
        p_items = len(policy_data["extracted_content"])
        p_tabs = policy_data["table_count"]
        p_codes = sum(len(c) for c in policy_data["codes_found"].values())
        total_items += p_items
        total_tables += p_tabs
        total_codes += p_codes

        logger.info(
            "       -> Extracted %d items, %d tables, %d codes across %d pages",
            p_items, p_tabs, p_codes, policy_data["page_count"]
        )

    # Conditional CPT mapping (gate: no explicit codes + procedural + not administrative)
    try:
        all_extracted_policies = _devoted_prepare_mapping(all_extracted_policies)
        all_extracted_policies, mapped_count = apply_cpt_mapping(all_extracted_policies, "devoted", log=logger)
        if mapped_count > 0:
            for p in all_extracted_policies:
                if p.get("mapping_status") == "mapped" and p.get("source_pdf"):
                    (output_dir / f"{Path(p['source_pdf']).stem}.json").write_text(
                        json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error("Automated CPT code mapping encountered an error: %s", e, exc_info=True)

    # Write consolidated master JSON
    master_json_path.write_text(
        json.dumps(all_extracted_policies, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Policies Processed:     %d", len(all_extracted_policies))
    logger.info("Total Content Items:    %d", total_items)
    logger.info("Total Tables Extracted: %d", total_tables)
    logger.info("Master JSON Written:    %s", master_json_path)
    logger.info("=" * 80)

    return all_extracted_policies


# ---------------------------------------------------------------------------
# CLI Entrypoint & Verification
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Devoted Health payment policy extraction script")
    parser.add_argument("--input-dir", type=str, default=str(POLICIES_DIR), help="Directory containing policy PDFs")
    parser.add_argument("--output-dir", type=str, default=str(OUTPUT_DIR), help="Directory for individual JSON outputs")
    parser.add_argument("--master-json", type=str, default=str(MASTER_JSON_PATH), help="Master consolidated JSON output")
    args = parser.parse_args()

    input_path = Path(args.input_dir).resolve()
    out_dir = Path(args.output_dir).resolve()
    master_json = Path(args.master_json).resolve()

    try:
        policies = process_all_policies(
            policies_dir=input_path,
            output_dir=out_dir,
            master_json_path=master_json,
        )
        if not policies:
            logger.error("No policies were extracted!")
            return 1

        print("\n" + "=" * 95)
        print("DEVOTED HEALTH CONTENT EXTRACTION SUMMARY")
        print("=" * 95)
        print(f"{'#':<3} | {'Policy Number':<16} | {'Pages':<6} | {'Items':<7} | {'Tables':<7} | {'Policy Title'}")
        print("-" * 95)
        for idx, p in enumerate(policies, 1):
            title = p["policy_title"][:42]
            print(
                f"{idx:<3} | {p['policy_number']:<16} | {p['page_count']:<6} | "
                f"{len(p['extracted_content']):<7} | {p['table_count']:<7} | {title}"
            )
        print("=" * 95)
        print(f"Master JSON:     {master_json} ({master_json.stat().st_size / (1024*1024):.2f} MB)")
        print(f"Individual JSON: {out_dir} ({len(list(out_dir.glob('*.json')))} files)")
        print("=" * 95 + "\n")
        return 0

    except Exception as exc:
        logger.exception("Extraction failed with error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
