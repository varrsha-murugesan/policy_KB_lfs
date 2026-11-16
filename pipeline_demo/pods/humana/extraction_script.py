#!/usr/bin/env python3
"""
Humana Payment & Claims Policy Extraction Engine
=================================================

High-accuracy, table-aware extraction pipeline for Humana Payment Integrity,
Reimbursement, and Claim Processing policies.

Features:
  1. Metadata Extraction:
     Payer-specific extraction of Subject, Application/LOB, Policy Number,
     Published Date, Revision/Effective Date, Related Policies, and Billing Class.
  2. Running Header/Footer & Boilerplate Stripping:
     Strips recurring headers, page numbers ("Page X of Y"), and disclaimer headers
     across pages to avoid prose contamination.
  3. Multi-Page Table Detection & Reconstruction:
     Detects tables across page breaks, removes phantom/empty columns, merges wrapped
     cells, preserves column headers, and generates both structured table matrices
     and per-row `table_row` elements.
  4. Hierarchical Section & Subsection Parsing:
     Tracks nested section hierarchy (Overview, Coverage, Reimbursement Guidelines,
     Definitions, etc.) and tags every content block with its full `hierarchy_path`.
  5. Code Scope Resolution:
     Extracts distinct CPT, HCPCS, Modifiers, ICD-10, and Revenue codes with
     proximity awareness and validation guards.
  6. Output Organization:
     - `Extraction_rules_humana.json`: Master consolidated JSON.
     - `extracted_contents_json/<policy_name>.json`: Granular per-policy files.

Usage:
  python Humana/extraction_script.py
  python Humana/extraction_script.py --input-dir Humana/humana_policies --limit 10
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
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


def _humana_prepare_mapping(policies):
    """Annotate each Humana policy with its mapping decision.
    The extractor's own 'explicit' mapped_cpt_codes pre-fill is kept (the billing generator reads it)
    but is ignored by the gate; only earlier KB/inferred entries count as 'already mapped'."""
    for p in policies:
        prior = any(isinstance(m, dict) and m.get("mapping_origin") not in (None, "explicit")
                    for m in (p.get("mapped_cpt_codes") or []))
        parts = _flatten_strings(p.get("extracted_content")) + _flatten_strings(p.get("reimbursement_guidelines")) \
            + _flatten_strings(p.get("coverage_criteria"))
        p["mapping_eligibility"] = decide_cpt_mapping(
            [p.get("policy_title")], " ".join(parts), p.get("CPT_CODES_USED"), existing_mapping=prior)
        if prior:
            p["mapping_status"] = "mapped"
    return policies

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("humana_extractor")

# ----------------------------------------------------------------------------
# Path & Directory Configuration
# ----------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
HUMANA_POLICIES_DIR = SCRIPT_DIR / "humana_policies"
OUTPUT_DIR = SCRIPT_DIR / "extracted_contents_json"
OUTPUT_RULES_FILE = SCRIPT_DIR / "extraction_rules_humana.json"

# ----------------------------------------------------------------------------
# Code Extraction Regex Patterns & Exclusions
# ----------------------------------------------------------------------------
CPT_HCPCS_PATTERN = re.compile(r"\b([0-9]{4}[0-9A-Z]|[A-V][0-9]{4})\b")
ICD10_PATTERN = re.compile(r"\b([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b")
REV_CODE_PATTERN = re.compile(r"\b(0[1-9][0-9]{2})\b")
MODIFIER_TOKEN_PATTERN = re.compile(r"\b([0-9]{2}|[A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\b")

# Medical modifiers catalog for contextual verification
KNOWN_MODIFIERS: Set[str] = {
    "22", "24", "25", "26", "50", "51", "52", "53", "54", "55", "56", "57",
    "58", "59", "62", "66", "76", "77", "78", "79", "80", "81", "82", "90",
    "91", "92", "93", "95", "AA", "AD", "AH", "AJ", "AM", "AQ", "AR", "AS",
    "AT", "BO", "CA", "CB", "CC", "CD", "CE", "CF", "CR", "CS", "CT", "ET",
    "EY", "FB", "FC", "FP", "FQ", "FR", "FX", "FY", "G0", "G8", "G9", "GA",
    "GC", "GE", "GG", "GH", "GJ", "GK", "GL", "GM", "GN", "GO", "GP", "GQ",
    "GR", "GS", "GT", "GV", "GW", "GX", "GY", "GZ", "HA", "HB", "HC", "HD",
    "HE", "HF", "HG", "HH", "HI", "HJ", "HK", "HL", "HM", "HN", "HO", "HP",
    "HQ", "HR", "HS", "HT", "HU", "HV", "HW", "HX", "HY", "HZ", "JA", "JB",
    "JC", "JD", "JE", "JF", "JG", "JK", "JL", "JW", "JZ", "KC", "KD", "KF",
    "KM", "KN", "KO", "KP", "KQ", "KR", "KS", "KT", "KX", "KZ", "LC", "LD",
    "LL", "LM", "LR", "LS", "LT", "MS", "NR", "NU", "PA", "PB", "PC", "PD",
    "PI", "PL", "PM", "PN", "PO", "PS", "PT", "Q0", "Q1", "Q2", "Q3", "Q4",
    "Q5", "Q6", "Q7", "Q8", "Q9", "QA", "QB", "QC", "QD", "QE", "QF", "QG",
    "QH", "QJ", "QK", "QL", "QM", "QN", "QP", "QQ", "QS", "QT", "QU", "QV",
    "QW", "QX", "QY", "QZ", "RA", "RB", "RC", "RD", "RE", "RI", "RR", "RT",
    "SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SJ", "SL", "SM", "SN",
    "TC", "TD", "TE", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TP",
    "TQ", "TR", "TS", "TT", "TU", "TV", "TW", "U1", "U2", "U3", "U4", "U5",
    "U6", "U7", "U8", "U9", "UA", "UB", "UC", "UD", "UE", "UF", "UG", "UH",
    "UJ", "UK", "UN", "UP", "UQ", "UR", "US", "VM", "VP", "VR", "XE", "XP",
    "XS", "XU"
}

# False-positive procedure codes (years, statutory codes, dates)
NON_CODE_TOKENS: Set[str] = {
    "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026",
    "1861", "1862", "1877", "1902", "1905", "1915", "1937",
}

# Known top-level sections in Humana Policies
HUMANA_KNOWN_SECTIONS = [
    "OVERVIEW",
    "MEDICARE ADVANTAGE PAYMENT POLICY",
    "COMMERCIAL PAYMENT POLICY",
    "PAYMENT POLICY",
    "COVERAGE",
    "COVERAGE CRITERIA",
    "REIMBURSEMENT GUIDELINES",
    "BILLING REQUIREMENTS",
    "BILLING GUIDELINES",
    "BILLING AND CODING",
    "CODING GUIDELINES",
    "COMMON MODIFIERS",
    "MODIFIER GUIDELINES",
    "DOCUMENTATION REQUIREMENTS",
    "PRIOR AUTHORIZATION",
    "REFERRAL REQUIREMENTS",
    "EXCLUSIONS",
    "NON-COVERED SERVICES",
    "LIMITATIONS",
    "DEFINITIONS",
    "RESOURCES",
    "REFERENCES",
    "REGULATORY REFERENCES",
    "DISCLAIMER",
    "HISTORY",
    "REVISION HISTORY",
]


# ============================================================================
# HELPER UTILITIES
# ============================================================================

def clean_text(text: Optional[str]) -> str:
    """Normalize unicode, collapse redundant whitespace, and strip."""
    if not text:
        return ""
    # Normalize common curly quotes, dashes, non-breaking spaces
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00a0", " ")
    )
    # Collapse multiple spaces and excessive newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def safe_slug(title: str, max_len: int = 60) -> str:
    """Create a safe filesystem filename slug from a policy title."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_")
    return slug[:max_len] if slug else "policy"


# ============================================================================
# METADATA EXTRACTION ENGINE
# ============================================================================

class HumanaMetadataParser:
    """Extracts policy frontmatter and administrative metadata from Humana PDF pages."""

    @staticmethod
    def extract_metadata(doc: pymupdf.Document, filename: str) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "policy_title": None,
            "policy_number": None,
            "published_date": None,
            "effective_date": None,
            "revision_date": None,
            "line_of_business": "Medicare Advantage Products",
            "billing_class": "Both",
            "related_policies": None,
            "source_pdf": filename,
            "total_pages": len(doc),
        }

        # Inspect up to first 2 pages for metadata block
        sample_text = ""
        for i in range(min(2, len(doc))):
            sample_text += doc[i].get_text("text") + "\n"

        lines = [clean_text(l) for l in sample_text.splitlines() if clean_text(l)]

        # 1. Subject / Title
        subject_match = re.search(r"Subject[:\s]+([^\n\r]+)", sample_text, re.IGNORECASE)
        if subject_match:
            meta["policy_title"] = clean_text(subject_match.group(1))
        else:
            # Fallback: scan lines near top of Page 1
            for l in lines[:10]:
                if l.lower() not in ("claims payment policy", "claims processing edit", "medical claims payment policy"):
                    if not any(k in l.lower() for k in ("application:", "published:", "policy number:", "page ")):
                        meta["policy_title"] = l
                        break

        # Fallback to cleaned filename if title still empty
        if not meta["policy_title"]:
            raw_stem = Path(filename).stem
            # Strip trailing hash like -2919704a
            raw_stem = re.sub(r"-[a-f0-9]{8}$", "", raw_stem, flags=re.IGNORECASE)
            raw_stem = raw_stem.replace("_Revised", "").replace("_New", "").replace("_", " ")
            meta["policy_title"] = raw_stem

        # 2. Policy Number
        pnum_match = re.search(
            r"Policy\s*(?:number|no\.?|#)[:\s]+([A-Za-z0-9\-_]+)", sample_text, re.IGNORECASE
        )
        if pnum_match:
            meta["policy_number"] = clean_text(pnum_match.group(1))
        else:
            # Match standard Humana policy IDs (e.g. CP2021001, LC10134SC0221)
            code_m = re.search(r"\b(CP20[0-9]{5}|LC[0-9]{5}[A-Z]{2}[0-9]{4}|SC[0-9]{3,4})\b", sample_text)
            if code_m:
                meta["policy_number"] = code_m.group(1)

        # 3. Application / Line of Business
        app_match = re.search(r"Application[:\s]+([^\n\r]+)", sample_text, re.IGNORECASE)
        if app_match:
            app_text = clean_text(app_match.group(1))
            meta["line_of_business"] = app_text
        elif "medicare advantage" in sample_text.lower():
            meta["line_of_business"] = "Medicare Advantage Products"
        elif "commercial" in sample_text.lower():
            meta["line_of_business"] = "Commercial Products"
        elif "medicaid" in sample_text.lower():
            meta["line_of_business"] = "Medicaid Products"

        # 4. Dates
        pub_match = re.search(r"Published(?:\s*date)?[:\s]+([A-Za-z0-9/\-]+)", sample_text, re.IGNORECASE)
        if pub_match:
            meta["published_date"] = clean_text(pub_match.group(1))

        rev_match = re.search(r"Revision\s*date[:\s]+([A-Za-z0-9/\-]+)", sample_text, re.IGNORECASE)
        if rev_match:
            meta["revision_date"] = clean_text(rev_match.group(1))

        eff_match = re.search(r"Effective\s*date[:\s]+([A-Za-z0-9/\-]+)", sample_text, re.IGNORECASE)
        if eff_match:
            meta["effective_date"] = clean_text(eff_match.group(1))

        # Effective date default to Published date or Revision date if omitted
        if not meta["effective_date"]:
            meta["effective_date"] = meta["published_date"] or meta["revision_date"]

        # 5. Related Policies
        rel_match = re.search(r"Related\s*policies[:\s]+([^\n\r]+)", sample_text, re.IGNORECASE)
        if rel_match:
            rel_txt = clean_text(rel_match.group(1))
            if rel_txt.upper() not in ("N/A", "NONE"):
                meta["related_policies"] = rel_txt

        # 6. Billing Class Detection
        text_lower = sample_text.lower()
        if "professional claims only" in text_lower or ("professional" in text_lower and "facility" not in text_lower):
            meta["billing_class"] = "Professional"
        elif "facility claims only" in text_lower or ("facility" in text_lower and "professional" not in text_lower):
            meta["billing_class"] = "Facility"
        else:
            meta["billing_class"] = "Both"

        return meta


# ============================================================================
# TABLE DETECTION & MULTI-PAGE STITCHING ENGINE
# ============================================================================

@dataclass
class ExtractedTable:
    title: str
    headers: List[str]
    rows: List[List[str]]
    page_start: int
    page_end: int
    column_count: int


class HumanaTableEngine:
    """Detects, cleans, normalizes, and stitches multi-page tables from Humana policy PDFs."""

    @staticmethod
    def extract_tables_from_doc(doc: pymupdf.Document) -> List[ExtractedTable]:
        raw_tables_by_page: List[Tuple[int, List[List[Optional[str]]]]] = []

        # 1. Extract raw tables per page
        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            try:
                tabs = page.find_tables()
                if tabs and tabs.tables:
                    for t in tabs.tables:
                        extracted = t.extract()
                        if extracted and len(extracted) >= 1:
                            raw_tables_by_page.append((page_num, extracted))
            except Exception as e:
                logger.debug("Table detection error on page %d: %s", page_num, e)

        if not raw_tables_by_page:
            return []

        # 2. Clean each raw table: eliminate phantom columns and normalize whitespace
        cleaned_page_tables: List[Tuple[int, List[List[str]]]] = []
        for pnum, matrix in raw_tables_by_page:
            cleaned_mat = HumanaTableEngine._clean_matrix(matrix)
            if cleaned_mat and len(cleaned_mat) >= 2:
                cleaned_page_tables.append((pnum, cleaned_mat))

        # 3. Stitch multi-page continuing tables
        stitched_tables: List[ExtractedTable] = []
        idx = 0
        while idx < len(cleaned_page_tables):
            cur_page, cur_mat = cleaned_page_tables[idx]
            headers = cur_mat[0]
            rows = cur_mat[1:]
            page_start = cur_page
            page_end = cur_page

            # Check if consecutive pages continue this table
            next_idx = idx + 1
            while next_idx < len(cleaned_page_tables):
                nxt_page, nxt_mat = cleaned_page_tables[next_idx]
                # Continuation conditions: consecutive page or 1 page gap, and identical column count
                if nxt_page == page_end + 1 and len(nxt_mat[0]) == len(headers):
                    nxt_headers = nxt_mat[0]
                    # If next table repeats the header, skip the header row; otherwise append all rows
                    if HumanaTableEngine._headers_similar(headers, nxt_headers):
                        rows.extend(nxt_mat[1:])
                    else:
                        rows.extend(nxt_mat)
                    page_end = nxt_page
                    next_idx += 1
                else:
                    break

            # Infer meaningful table title from headers
            table_title = HumanaTableEngine._infer_table_title(headers)

            stitched_tables.append(
                ExtractedTable(
                    title=table_title,
                    headers=headers,
                    rows=rows,
                    page_start=page_start,
                    page_end=page_end,
                    column_count=len(headers),
                )
            )
            idx = next_idx

        return stitched_tables

    @staticmethod
    def _clean_matrix(matrix: List[List[Optional[str]]]) -> List[List[str]]:
        """Remove empty phantom columns and clean whitespace inside table cells."""
        if not matrix:
            return []

        col_count = max(len(r) for r in matrix)
        # Find which columns contain non-empty text anywhere in the table
        active_cols = []
        for c in range(col_count):
            has_val = any(
                r[c] is not None and clean_text(str(r[c])) != ""
                for r in matrix
                if c < len(r)
            )
            if has_val:
                active_cols.append(c)

        if not active_cols:
            return []

        cleaned: List[List[str]] = []
        for r in matrix:
            cleaned_row = []
            for c in active_cols:
                val = r[c] if c < len(r) and r[c] is not None else ""
                cleaned_row.append(clean_text(str(val)))
            cleaned.append(cleaned_row)

        # Merge multiline header if Row 0 has blanks filled by Row 1
        if len(cleaned) >= 2:
            h0, h1 = cleaned[0], cleaned[1]
            if any(h == "" for h in h0) and any(h != "" for h in h1):
                merged_header = []
                for c in range(len(h0)):
                    m = f"{h0[c]} {h1[c]}".strip()
                    merged_header.append(m)
                cleaned[0] = merged_header
                cleaned.pop(1)

        return cleaned

    @staticmethod
    def _headers_similar(h1: List[str], h2: List[str]) -> bool:
        if len(h1) != len(h2):
            return False
        matches = sum(1 for a, b in zip(h1, h2) if a.lower() == b.lower() and a != "")
        return matches >= max(1, len(h1) // 2)

    @staticmethod
    def _infer_table_title(headers: List[str]) -> str:
        h_str = " ".join(headers).lower()
        if "modifier" in h_str:
            return "Modifier Reimbursement and Guidelines Table"
        elif "cpt" in h_str or "hcpcs" in h_str or "code" in h_str:
            return "Applicable Procedure and Service Codes"
        elif "diagnosis" in h_str or "icd" in h_str:
            return "Applicable Diagnosis Criteria Codes"
        elif "percentage" in h_str or "reduction" in h_str or "mppr" in h_str:
            return "Multiple Procedure Payment Reduction Matrix"
        return "Policy Schedule and Criteria Table"


# ============================================================================
# SECTION & CONTENT BLOCK HIERARCHY PARSER
# ============================================================================

class HumanaSectionParser:
    """Parses text sections, subsections, and running headers with hierarchy tracking."""

    # Headers and recurring artifacts to strip from pages
    RUNNING_HEADER_PATTERNS = [
        re.compile(r"^Claims Payment Policy\s*$", re.IGNORECASE),
        re.compile(r"^Claims Processing Edit\s*$", re.IGNORECASE),
        re.compile(r"^Medical Claims Payment Policy\s*$", re.IGNORECASE),
        re.compile(r"^Subject[:\s]+[^\n\r]+$", re.IGNORECASE),
        re.compile(r"^Policy\s*(?:number|no\.?|#)[:\s]+[^\n\r]+$", re.IGNORECASE),
        re.compile(r"^Please see the disclaimer on the first page\.?\s*$", re.IGNORECASE),
        re.compile(r"^Page \d+ of \d+\s*$", re.IGNORECASE),
        re.compile(r"^When printed, this version becomes uncontrolled\.?.*$", re.IGNORECASE),
    ]

    @classmethod
    def clean_page_text(cls, raw_text: str, page_num: int) -> List[str]:
        """Strip running headers, page markers, and disclaimers from a single page's text lines."""
        lines = [clean_text(l) for l in raw_text.splitlines()]
        cleaned_lines = []

        in_disclaimer = False
        for l in lines:
            if not l:
                continue

            # Strip running header lines
            if any(pat.match(l) for pat in cls.RUNNING_HEADER_PATTERNS):
                continue

            # Skip disclaimer on page 1
            if page_num == 1 and l.lower().startswith("disclaimer:"):
                in_disclaimer = True
                continue
            if in_disclaimer:
                if any(sec in l.upper() for sec in HUMANA_KNOWN_SECTIONS):
                    in_disclaimer = False
                else:
                    continue

            cleaned_lines.append(l)

        return cleaned_lines

    @classmethod
    def parse_blocks_from_doc(
        cls, doc: pymupdf.Document, tables: List[ExtractedTable]
    ) -> List[Dict[str, Any]]:
        """Extract hierarchically organized prose blocks, bullets, and tables."""
        blocks: List[Dict[str, Any]] = []

        cur_section: str = "Overview"
        cur_subsection: Optional[str] = None
        hierarchy_stack: List[str] = ["Overview"]

        # Track table pages to avoid duplicating table text as prose paragraphs
        table_page_ranges = [(t.page_start, t.page_end) for t in tables]

        for page_idx, page in enumerate(doc):
            page_num = page_idx + 1
            raw_text = page.get_text("text")
            lines = cls.clean_page_text(raw_text, page_num)

            # Process prose lines into paragraphs and sections
            paragraph_buffer: List[str] = []

            def flush_buffer(p_type="paragraph"):
                nonlocal paragraph_buffer
                if not paragraph_buffer:
                    return
                content = clean_text(" ".join(paragraph_buffer))
                paragraph_buffer = []
                if len(content) < 3:
                    return

                # Extract codes in this block
                cpts = list(OrderedDict.fromkeys(CPT_HCPCS_PATTERN.findall(content)))
                cpts = [c for c in cpts if c not in NON_CODE_TOKENS]

                path = " > ".join(hierarchy_stack)
                blocks.append(
                    {
                        "title": cur_subsection or cur_section,
                        "section": cur_section,
                        "subsection": cur_subsection,
                        "hierarchy_path": path,
                        "page_number": page_num,
                        "page_start": page_num,
                        "page_end": page_num,
                        "content": content,
                        "codes": cpts,
                        "table_data": None,
                        "content_type": p_type,
                    }
                )

            for line in lines:
                # 1. Check for Major Section Header
                line_upper = line.upper().strip()
                matched_sec = None
                for known_sec in HUMANA_KNOWN_SECTIONS:
                    if line_upper == known_sec or line_upper == f"{known_sec}:":
                        matched_sec = known_sec.title()
                        break

                if matched_sec:
                    flush_buffer()
                    cur_section = matched_sec
                    cur_subsection = None
                    hierarchy_stack = [cur_section]
                    continue

                # 2. Check for Subsection Header (short bold/standalone title line)
                if (
                    len(line) < 65
                    and not line.endswith((".", ";", ":", ","))
                    and not line.startswith(("•", "-", "*", "1.", "2.", "3."))
                    and line[0].isupper()
                    and any(c.islower() for c in line)
                ):
                    flush_buffer()
                    cur_subsection = line
                    hierarchy_stack = [cur_section, cur_subsection]
                    continue

                # 3. Check for Bullet Point
                if line.startswith(("•", "-", "▪", "*", "–")) or re.match(r"^\d+\.\s+", line):
                    flush_buffer()
                    paragraph_buffer.append(line)
                    flush_buffer("bullet_point")
                    continue

                # 4. Normal paragraph text line
                paragraph_buffer.append(line)

            flush_buffer()

        # 5. Insert Extracted Tables as structured `table_row` and table summary blocks
        for t in tables:
            headers = t.headers
            for row in t.rows:
                # Create row mapping
                row_dict = {}
                row_content_parts = []
                for h, val in zip(headers, row):
                    if h:
                        row_dict[h] = val
                        row_content_parts.append(f"{h}: {val}")
                    else:
                        row_content_parts.append(val)

                row_content = f"[{t.title}] " + " | ".join(row_content_parts)
                cpts = list(OrderedDict.fromkeys(CPT_HCPCS_PATTERN.findall(row_content)))
                cpts = [c for c in cpts if c not in NON_CODE_TOKENS]

                blocks.append(
                    {
                        "title": t.title,
                        "section": cur_section or "Tables and Code Schedules",
                        "subsection": t.title,
                        "hierarchy_path": f"{cur_section or 'Tables'} > {t.title}",
                        "page_number": t.page_start,
                        "page_start": t.page_start,
                        "page_end": t.page_end,
                        "content": row_content,
                        "codes": cpts,
                        "table_data": {
                            "columns": headers,
                            "row": row_dict,
                        },
                        "content_type": "table_row",
                    }
                )

        return blocks


# ============================================================================
# MASTER SINGLE-POLICY EXTRACTION ORCHESTRATOR
# ============================================================================

class HumanaPolicyExtractor:
    """Orchestrates metadata, tables, sections, and code indexing for a single Humana PDF."""

    @staticmethod
    def extract_policy(pdf_path: Path) -> Dict[str, Any]:
        doc = pymupdf.open(str(pdf_path))
        filename = pdf_path.name

        # 1. Metadata
        meta = HumanaMetadataParser.extract_metadata(doc, filename)

        # 2. Tables
        tables = HumanaTableEngine.extract_tables_from_doc(doc)

        # 3. Content Blocks & Hierarchy
        content_blocks = HumanaSectionParser.parse_blocks_from_doc(doc, tables)

        # 4. Document-wide Distinct Codes
        all_text = " ".join([b.get("content", "") for b in content_blocks])
        cpt_codes = list(OrderedDict.fromkeys(CPT_HCPCS_PATTERN.findall(all_text)))
        cpt_codes = [c for c in cpt_codes if c not in NON_CODE_TOKENS and len(c) == 5]

        icd_codes = list(OrderedDict.fromkeys(ICD10_PATTERN.findall(all_text)))

        # Contextual Modifier extraction
        modifiers: List[str] = []
        for m in MODIFIER_TOKEN_PATTERN.findall(all_text):
            if m.upper() in KNOWN_MODIFIERS and m.upper() not in modifiers:
                modifiers.append(m.upper())

        # Construct mapped CPT code records for downstream rule generation
        mapped_cpts = []
        for c in cpt_codes:
            mapped_cpts.append(
                {
                    "code": c,
                    "code_system": "CPT/HCPCS",
                    "description": "",
                    "confidence_tier": "A",
                    "score": 100.0,
                    "mapping_origin": "explicit",
                    "best_method": "document_table_or_prose",
                }
            )

        # 5. Section-keyed dictionary views (matching UHC interoperability)
        def get_section_text(sec_name: str) -> str:
            matches = [
                b["content"]
                for b in content_blocks
                if sec_name.lower() in (b.get("section") or "").lower()
                or sec_name.lower() in (b.get("title") or "").lower()
            ]
            return "\n\n".join(matches)

        rg_text = get_section_text("reimbursement") or get_section_text("payment policy")
        cov_text = get_section_text("coverage")
        def_text = get_section_text("definition")

        policy_obj: Dict[str, Any] = {
            "policy_title": meta["policy_title"],
            "policy_number": meta["policy_number"],
            "effective_date": meta["effective_date"],
            "published_date": meta["published_date"],
            "revision_date": meta["revision_date"],
            "source_pdf": filename,
            "line_of_business": meta["line_of_business"],
            "billing_class": meta["billing_class"],
            "related_policies": meta["related_policies"],
            "total_pages": meta["total_pages"],
            "table_count": len(tables),
            "block_count": len(content_blocks),
            "CPT_CODES_USED": cpt_codes,
            "MODIFIERS_USED": modifiers,
            "ICD_CODES_USED": icd_codes,
            "mapped_cpt_codes": mapped_cpts,
            "extracted_content": content_blocks,
            # Structured section views for UHC-style consumers
            "reimbursement_guidelines": {
                "title": "Reimbursement Guidelines",
                "content": rg_text or all_text[:2000],
            },
            "coverage_criteria": {
                "title": "Coverage Criteria",
                "content": cov_text,
            },
            "definitions": {
                "title": "Definitions",
                "content": def_text,
            },
        }

        doc.close()
        return policy_obj


# ============================================================================
# BATCH EXECUTION & EXPORT PIPELINE
# ============================================================================

def run_extraction_batch(
    input_dir: Path,
    output_dir: Path,
    master_json_path: Path,
    limit: Optional[int] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Process all PDF policy documents in input_dir and export structured JSONs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    master_json_path.parent.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        logger.warning("No PDF policy files found in %s", input_dir)
        return []

    if limit:
        pdf_files = pdf_files[:limit]

    logger.info("Found %d PDF policies to extract from %s", len(pdf_files), input_dir)

    all_policies: List[Dict[str, Any]] = []
    start_time = time.time()

    total_blocks = 0
    total_tables = 0
    total_codes = 0

    for idx, pdf_path in enumerate(pdf_files, 1):
        filename = pdf_path.name
        slug = safe_slug(Path(filename).stem)
        indiv_json_path = output_dir / f"{slug}.json"

        # Check existing cached JSON unless force requested
        if indiv_json_path.exists() and not force:
            try:
                with open(indiv_json_path, "r", encoding="utf-8") as f:
                    policy_data = json.load(f)
                if not is_target_lob(policy_data.get("line_of_business")):
                    logger.info("[%d/%d] SKIPPED (not Medicare Advantage: %s): %s", idx, len(pdf_files), policy_data.get("line_of_business"), filename)
                    continue
                all_policies.append(policy_data)
                total_blocks += policy_data.get("block_count", 0)
                total_tables += policy_data.get("table_count", 0)
                total_codes += len(policy_data.get("CPT_CODES_USED", []))
                logger.info("[%d/%d] CACHED: %s", idx, len(pdf_files), filename)
                continue
            except Exception:
                pass

        try:
            logger.info("[%d/%d] Extracting: %s...", idx, len(pdf_files), filename)
            policy_data = HumanaPolicyExtractor.extract_policy(pdf_path)
            if not is_target_lob(policy_data.get("line_of_business")):
                logger.info("[%d/%d] SKIPPED (not Medicare Advantage: %s): %s", idx, len(pdf_files), policy_data.get("line_of_business"), filename)
                continue

            # Write individual JSON
            with open(indiv_json_path, "w", encoding="utf-8") as f:
                json.dump(policy_data, f, indent=2, ensure_ascii=False)

            all_policies.append(policy_data)

            total_blocks += policy_data.get("block_count", 0)
            total_tables += policy_data.get("table_count", 0)
            total_codes += len(policy_data.get("CPT_CODES_USED", []))

        except Exception as e:
            logger.error("Failed to extract %s: %s", filename, e, exc_info=True)

    # ------------------------------------------------------------------------
    # Conditional CPT mapping (gate: no explicit codes + procedural + not administrative)
    # ------------------------------------------------------------------------
    try:
        all_policies = _humana_prepare_mapping(all_policies)
        all_policies, mapped_count = apply_cpt_mapping(all_policies, "humana", log=logger)
        if mapped_count > 0:
            for p in all_policies:
                if p.get("mapping_status") == "mapped" and p.get("source_pdf"):
                    slug = safe_slug(Path(p["source_pdf"]).stem)
                    with open(output_dir / f"{slug}.json", "w", encoding="utf-8") as f:
                        json.dump(p, f, indent=2, ensure_ascii=False)
            logger.info("Successfully updated %d policies with KB CPT mappings", mapped_count)
    except Exception as e:
        logger.error("Automated CPT code mapping encountered an error: %s", e, exc_info=True)

    # 1. Export Master Consolidated JSON
    logger.info("Writing master consolidated JSON to %s...", master_json_path)
    with open(master_json_path, "w", encoding="utf-8") as f:
        json.dump(all_policies, f, indent=2, ensure_ascii=False)

    elapsed = time.time() - start_time
    logger.info("=" * 65)
    logger.info("EXTRACTION PIPELINE COMPLETED SUCCESSFULLY IN %.2fs", elapsed)
    logger.info("  Policies Processed : %d", len(all_policies))
    logger.info("  Total Content Blocks: %d", total_blocks)
    logger.info("  Tables Extracted   : %d", total_tables)
    logger.info("  Distinct CPT Codes : %d", total_codes)
    logger.info("  Individual JSONs   : %s", output_dir)
    logger.info("  Master Rules File  : %s", master_json_path)
    logger.info("=" * 65)

    return all_policies


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Humana Payment & Claims Policy High-Accuracy Extraction Pipeline"
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default=str(HUMANA_POLICIES_DIR),
        help=f"Directory containing Humana policy PDFs (default: {HUMANA_POLICIES_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(OUTPUT_DIR),
        help=f"Directory for individual policy JSON outputs (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--master-json",
        type=str,
        default=str(OUTPUT_RULES_FILE),
        help=f"Master consolidated JSON output file (default: {OUTPUT_RULES_FILE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of policies to process (for debugging/testing)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-extraction of all policies, ignoring cached individual JSONs",
    )

    args = parser.parse_args()

    input_path = Path(args.input_dir).resolve()
    out_dir = Path(args.output_dir).resolve()
    master_json = Path(args.master_json).resolve()

    if not input_path.exists():
        logger.error("Input directory %s does not exist!", input_path)
        return 1

    policies = run_extraction_batch(
        input_dir=input_path,
        output_dir=out_dir,
        master_json_path=master_json,
        limit=args.limit,
        force=args.force,
    )

    return 0 if policies else 1


if __name__ == "__main__":
    sys.exit(main())

