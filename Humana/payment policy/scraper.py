#!/usr/bin/env python3
"""
Humana Multi-Source Payment Policy & Claim-Editing Harvester
===========================================================

Multi-Source Payer Policy Intelligence Platform - Humana Harvester.

Sources crawled (Strict Scope: Payment & Reimbursement ONLY):
1. Claims Payment Policies:
   https://provider.humana.com/coverage-claims/claims-payment-policies
2. Claims Processing Edits:
   https://provider.humana.com/coverage-claims/processing-edits
3. Claim Processing Edits Search:
   https://provider.humana.com/coverage-claims/processing-edits/claim-processing-edits-search
4. Code Edit Announcements:
   https://provider.humana.com/coverage-claims/code-edit-announcements
5. Provider Payment Integrity:
   https://provider.humana.com/coverage-claims/payment-integrity
6. Postpayment Review Policy:
   https://provider.humana.com/coverage-claims/payment-integrity/post-payment-review
7. Claim Payments Information:
   https://provider.humana.com/coverage-claims/claims-payment-information
8. State-specific: South Carolina Medicaid Provider Claims:
   https://provider.humana.com/medicaid/south-carolina-medicaid/claims

Target CPT Codes Analyzed:
- 15271, 15274, 15275, 15278 (Exact matching and range expansion, e.g., 15271-15278)
- Special detection for Rule SC174 (High-cost skin substitute application procedures).

Output Artifacts:
- PDFs: ./<out>/pdfs/
- Audit CSV: ./<out>/audit_<run_id>.csv
- Latest Policies: ./<out>/latest_policies.csv
- Target CPT Matches: ./<out>/target_cpt_matches.json & .csv
- Source Discovery Report: ./<out>/source_discovery_report.json
- State File: ./<out>/state.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from pypdf import PdfReader

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ORIGIN = "https://provider.humana.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
MAX_PDF_BYTES = 250 * 1024 * 1024
log = logging.getLogger("humana")

# ----------------------------------------------------------------------------
# Multi-Source Payment & Reimbursement Configuration
# ----------------------------------------------------------------------------
PAYMENT_SOURCES = [
    {
        "url": "https://provider.humana.com/coverage-claims/claims-payment-policies",
        "category": "Claims Payment Policy",
        "subtype": "Claims Payment Policy",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": True,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/processing-edits",
        "category": "Claim Editing Rules",
        "subtype": "Processing Edits Overview",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": False,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/processing-edits/claim-processing-edits-search",
        "category": "Claim Editing Rules",
        "subtype": "Claim Processing Edits Search",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": True,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/code-edit-announcements",
        "category": "Payment Process Announcement",
        "subtype": "Code Edit Announcements",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": False,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/payment-integrity",
        "category": "Payment Integrity / Postpayment Review",
        "subtype": "Payment Integrity Overview",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": False,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/payment-integrity/post-payment-review",
        "category": "Payment Integrity / Postpayment Review",
        "subtype": "Postpayment Review Policy",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": False,
    },
    {
        "url": "https://provider.humana.com/coverage-claims/claims-payment-information",
        "category": "Reimbursement Guideline",
        "subtype": "Claims Payment Information",
        "jurisdiction": "National",
        "lob": "Commercial / Medicare / Medicaid",
        "render_browser": False,
    },
    {
        "url": "https://provider.humana.com/medicaid/south-carolina-medicaid/claims",
        "category": "Claim Editing Rules",
        "subtype": "State Medicaid Code Editing Rules",
        "jurisdiction": "South Carolina",
        "lob": "Medicaid",
        "render_browser": False,
    },
]

# Strict domain and URL scoping
ALLOWED_HOSTS = {
    "provider.humana.com",
    "assets.humana.com",
    "dctm.humana.com",
    "www.humana.com",
    "es-provider.humana.com",
}

# Strict exclusion list: NEVER crawl Medical Coverage Policies, Dental, Vision, Careers
DISALLOWED_PATHS = (
    "/coverage-policies",
    "/medical-coverage",
    "/careers",
    "/about",
    "/employer",
    "/individual",
    "/pharmacy",
    "/dental",
    "/vision",
    "/contact-us",
)

TARGET_CPT_CODES = {"15271", "15274", "15275", "15278"}


# ----------------------------------------------------------------------------
# Text / Date / Helper Functions
# ----------------------------------------------------------------------------
def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def strip_tags(s: str) -> str:
    return clean(re.sub(r"<[^>]+>", " ", s or ""))


_MONTH = (
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
MONTH_NUM = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
DATE_PATTERNS = [
    re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I),
    re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(rf"\b{_MONTH}\.?,?\s+(\d{{4}})\b", re.I),
]
LABEL_RE = re.compile(
    r"(revis|effective|updated|publish|last review|modified|posted|as of|released)", re.I
)
DATE_KEY_RE = re.compile(r"(date|effective|revis|updated|modified|publish|posted|released)", re.I)


def find_dates(text: str):
    out = []
    for i, p in enumerate(DATE_PATTERNS):
        for m in p.finditer(text or ""):
            try:
                if i == 0:
                    mo, d, y = MONTH_NUM[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
                elif i == 1:
                    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif i == 2:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    mo, d, y = MONTH_NUM[m.group(1)[:3].lower()], 1, int(m.group(2))
                if 1990 <= y <= 2100:
                    out.append((date(y, mo, d).isoformat(), m.start(), m.end()))
            except (ValueError, KeyError):
                continue
    return out


def best_date(text: str, labeled_only: bool = False):
    ds = find_dates(text)
    labeled = [iso for iso, s, _ in ds if LABEL_RE.search(text[max(0, s - 40):s])]
    if labeled:
        return max(labeled), 3
    if ds and not labeled_only:
        return max(d[0] for d in ds), 2
    return None, 0


def coerce_date(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        if 1e9 < x < 4.2e9:
            return datetime.fromtimestamp(x, timezone.utc).date().isoformat()
        return None
    if isinstance(v, str):
        return best_date(v)[0]
    return None


def key_rank(key: str) -> int:
    k = key.lower()
    if re.search(r"effective|revis|publish|release", k):
        return 3
    if "modified" in k or "updated" in k:
        return 1
    return 2


# ----------------------------------------------------------------------------
# URL & Document Classification Helpers
# ----------------------------------------------------------------------------
def is_pdf_url(s: Any) -> bool:
    """Humana asset URLs often end in '...pdf' with NO dot or point to Documentum (dctm)."""
    if not isinstance(s, str) or len(s) > 2500:
        return False
    s = s.strip()
    if not re.match(r"^(https?://|/)[^\s]+$", s):
        return False
    p = urlparse(s).path.lower()
    return (
        p.endswith(".pdf")
        or p.endswith("pdf")
        or "/is/content/" in p
        or "dctm.humana.com" in s.lower()
        or "chronicleid" in s.lower()
        or "/mentor/web/" in p
    )


def is_allowed_humana_url(url: str) -> bool:
    """Validate that URL belongs to Humana and does not enter forbidden coverage policy areas."""
    try:
        p = urlparse(url)
        host = p.netloc.lower().split(":")[0]
        if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
            return False
        path = p.path.lower()
        if any(path.startswith(bad) for bad in DISALLOWED_PATHS):
            return False
        return True
    except Exception:
        return False


def is_relevant_payment_link(url: str, target_lob: str = "medicare") -> bool:
    """Check if an HTML URL is a relevant payment/reimbursement subpage."""
    if not is_allowed_humana_url(url):
        return False
    p = urlparse(url).path.lower()
    if target_lob == "medicare" and "/medicaid/" in p and "medicare" not in p:
        return False
    relevant_subpaths = [
        "/coverage-claims/",
        "/processing-edits",
        "/payment-integrity",
        "/claims",
    ]
    if target_lob != "medicare":
        relevant_subpaths.append("/medicaid/")
    return any(sub in p for sub in relevant_subpaths) and not is_pdf_url(url)


def norm_url(u: str) -> str:
    p = urlparse(u)
    q = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in {"download", "dl"} and not k.lower().startswith("utm_")
    ]
    if "dctm.humana.com" in p.netloc.lower():
        cid = dict(parse_qsl(p.query, keep_blank_values=True)).get("chronicleID")
        if cid:
            q = [("chronicleID", cid)]
    path = quote(unquote(p.path), safe="/%:@!$&'()*+,;=-._~")
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))


GENERIC_TITLE = re.compile(
    r"^(pdf|download|view|open|read more|learn more|click here|link|here|details)\b", re.I
)
_STOP = re.compile(
    r"\b(revised|revision|rev|effective|updated|update|final|draft|copy|version|ver|v\d+(?:\.\d+)*)\b"
)


def policy_key(title: str, url: str) -> str:
    """Stable identity for 'the same policy' across versions."""
    s = unquote(title or Path(unquote(urlparse(url).path)).name).lower()
    s = re.sub(r"\.pdf$", "", s)
    if not title:
        s = re.sub(r"pdf$", "", s)
    for pat in DATE_PATTERNS:
        s = pat.sub(" ", s)
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"\b(?:19|20)\d{2}\b", " ", s)
    s = re.sub(r"\b\d{6,8}\b", " ", s)
    s = _STOP.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s or "unknown-" + hashlib.sha1(url.encode()).hexdigest()[:8]


def version_tuple(title: str, url: str) -> tuple:
    s = f"{title} {unquote(urlparse(url).path)}"
    m = re.search(r"(?:\bv(?:ersion)?|\brev(?:ision)?)[\s._-]*(\d+(?:\.\d+)*)", s, re.I)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def safe_filename(url: str, title: str = "") -> str:
    base = title or unquote(Path(urlparse(url).path).name) or "policy"
    name = re.sub(r"\.pdf$", "", base, flags=re.I)
    name = re.sub(r"pdf$", "", name) if not name.lower().endswith(".pdf") else name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:80] or "policy"
    return f"{slug}-{hashlib.sha1(norm_url(url).encode()).hexdigest()[:8]}.pdf"


def is_lob_eligible(
    lob: str, title: str = "", url: str = "", target_lob: str = "medicare"
) -> Tuple[bool, str]:
    """
    Check whether a policy candidate matches the target line of business (LOB).

    Target 'medicare':
    - ALLOWED:
      - Medicare Advantage / Medicare Part B
      - Commercial / Medicare / Medicaid (Humana national reimbursement policies)
      - Medicare and Medicaid dual-eligible / D-SNP policies
    - STRICTLY EXCLUDED:
      - Pure Medicaid policies (e.g. South Carolina Medicaid, Indiana PathWays, state Medicaid rules)
      - Pure Commercial policies that do not cover Medicare
    """
    if target_lob == "all":
        return True, "allowed_all"

    lob_lower = (lob or "").lower()
    title_lower = (title or "").lower()
    url_lower = (url or "").lower()
    combined = f"{lob_lower} {title_lower} {url_lower}"

    has_medicare = "medicare" in combined
    has_medicaid = (
        "medicaid" in combined
        or "south_carolina" in combined
        or "south carolina" in combined
        or "pathways" in combined
        or "epsdt" in combined
    )

    # 1. Policies covering Medicare (Medicare-only or Medicare & Medicaid dual)
    if has_medicare:
        return True, "medicare_or_dual"

    # 2. Pure Medicaid policies (Medicaid without Medicare) -> strictly excluded
    if has_medicaid or lob_lower == "medicaid":
        return False, "medicaid_only_excluded"

    # 3. If source LOB includes Medicare (e.g. Commercial / Medicare / Medicaid)
    if "medicare" in lob_lower:
        return True, "medicare_lob"

    # 4. Pure commercial or other LOB without Medicare
    if "commercial" in lob_lower and not has_medicare:
        return False, "commercial_only_excluded"

    # Default: Standard Humana payment policies apply to Commercial / Medicare / Medicaid
    return True, "medicare_or_dual"


def infer_metadata(url: str, title: str, source_meta: dict) -> dict:
    """Infer category, subtype, jurisdiction, and line of business from URL and title."""
    u_lower = url.lower()
    t_lower = (title or "").lower()

    category = source_meta.get("category", "Claims Payment Policy")
    subtype = source_meta.get("subtype", "Payment Policy")
    jurisdiction = source_meta.get("jurisdiction", "National")
    lob = source_meta.get("lob", "Commercial / Medicare / Medicaid")

    has_medicare = "medicare" in u_lower or "medicare" in t_lower
    has_medicaid = (
        "medicaid" in u_lower
        or "medicaid" in t_lower
        or "south_carolina" in u_lower
        or "south carolina" in t_lower
    )

    if "south_carolina" in u_lower or "south carolina" in t_lower:
        jurisdiction = "South Carolina"
        category = "Claim Editing Rules"
        subtype = "State Medicaid Code Editing Rules"
        lob = "Medicare and Medicaid" if has_medicare else "Medicaid"
    elif has_medicare and has_medicaid:
        lob = "Medicare and Medicaid"
    elif has_medicaid:
        lob = "Medicaid"
        category = "Claim Editing Rules"
    elif has_medicare:
        lob = "Medicare Advantage"
    elif "post-payment" in u_lower or "postpayment" in t_lower:
        category = "Payment Integrity / Postpayment Review"
        subtype = "Postpayment Review Policy"
    elif "processing-edits" in u_lower or "code-edit" in u_lower:
        category = "Claim Editing Rules"
    elif "announcement" in u_lower or "announcement" in t_lower:
        category = "Payment Process Announcement"

    return {
        "category": category,
        "subtype": subtype,
        "jurisdiction": jurisdiction,
        "lob": lob,
    }


# ----------------------------------------------------------------------------
# Candidate Registry
# ----------------------------------------------------------------------------
@dataclass
class Candidate:
    url: str
    key: str
    title: str = ""
    source_url: str = ""
    category: str = "Claims Payment Policy"
    subtype: str = ""
    jurisdiction: str = "National"
    lob: str = "Commercial / Medicare / Medicaid"
    date: str = ""
    date_rank: int = 0
    date_source: str = ""
    methods: set = field(default_factory=set)
    seen: int = 0


def clean_title_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    stem = Path(path).name
    stem = re.sub(r'\.pdf$', '', stem, flags=re.I)
    stem = re.sub(r'pdf$', '', stem, flags=re.I)
    stem = re.sub(r'^[A-Z0-9]+_[A-Z0-9]+_', '', stem)
    stem = re.sub(r'^[A-Z0-9_\-]+_', '', stem)
    title = re.sub(r'[_\-]+', ' ', stem).strip()
    return title.title() if len(title) > 3 else ""


def better_title(old: str, new: str, url: str = "") -> str:
    new = clean(new)
    if not new or is_pdf_url(new) or len(new) > 300:
        if not old and url:
            return clean_title_from_url(url)
        return old
    if not old:
        return new
    if GENERIC_TITLE.match(old) and not GENERIC_TITLE.match(new):
        return new
    if GENERIC_TITLE.match(new):
        return old
    return new if len(new) > len(old) and len(new) <= 200 else old



class Registry:
    def __init__(self):
        self.items: dict[str, Candidate] = {}

    def __len__(self):
        return len(self.items)

    def add(
        self,
        raw: str,
        title: str = "",
        date_iso: Optional[str] = None,
        drank: int = 0,
        dsrc: str = "",
        method: str = "",
        base: str = ORIGIN,
        source_meta: Optional[dict] = None,
    ) -> bool:
        url = urljoin(base, raw.strip()).split("#")[0]
        if not is_allowed_humana_url(url):
            log.debug("skipping off-site or disallowed URL %s", url)
            return False
        k = norm_url(url)
        meta = infer_metadata(url, title, source_meta or {})
        c = self.items.get(k)
        if c is None:
            c = self.items[k] = Candidate(
                url=url,
                key=k,
                source_url=base,
                category=meta["category"],
                subtype=meta["subtype"],
                jurisdiction=meta["jurisdiction"],
                lob=meta["lob"],
            )
        else:
            c.url = url
            if not c.source_url:
                c.source_url = base

        c.seen += 1
        c.methods.add(method)
        c.title = better_title(c.title, title, url)
        if date_iso and (drank > c.date_rank or (drank == c.date_rank and date_iso > c.date)):

            c.date, c.date_rank, c.date_source = date_iso, drank, dsrc
        return True


# ----------------------------------------------------------------------------
# HTML & JSON Extraction with Multi-Source Support
# ----------------------------------------------------------------------------
TITLE_KEYS = (
    "title",
    "name",
    "policyname",
    "policytitle",
    "displayname",
    "label",
    "heading",
    "headline",
    "linktext",
    "text",
    "description",
    "filename",
)
A_TAG = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
EMBEDDED_JSON_PDF_RE = re.compile(
    r'(?:https?:\\?/\\?/[^\s"\'<>]+\.pdf|https?:\\?/\\?/assets\.humana\.com\\?/is\\?/content\\?/humana\\?/[^\s"\'<>]+|/is/content/humana/[^\s"\'<>]+)',
    re.I,
)


def extract_links_from_content(
    html: str, reg: Registry, method: str, base_url: str, source_meta: dict
) -> Tuple[int, Set[str]]:
    """
    Extract PDF candidates AND discover internal sub-page HTML links for recursive crawl.
    Robustly handles HTML, unescapes JSON/Content Fragment unicode strings.
    """
    pdf_count = 0
    sub_pages: Set[str] = set()

    # 1. Standard HTML <a> tags
    for m in A_TAG.finditer(html or ""):
        href, text = m.group(1).replace("\\", "").strip(), strip_tags(m.group(2))
        full = urljoin(base_url, href)
        if is_pdf_url(href) or is_pdf_url(full):
            ctx = strip_tags(html[max(0, m.start() - 300) : m.end() + 300])
            d, r = best_date(ctx)
            pdf_count += reg.add(
                full, text, d, r, "html:context" if d else "", method, base_url, source_meta
            )
        elif is_relevant_payment_link(full):
            sub_pages.add(full)

    # 2. Embedded Content Fragments / Escaped JSON inside HTML
    unescaped = html
    try:
        unescaped = html.encode("utf-8").decode("unicode_escape", errors="ignore")
    except Exception:
        pass

    # Find raw asset URLs in JSON/text
    for m in EMBEDDED_JSON_PDF_RE.finditer(unescaped):
        raw_url = m.group(0).replace("\\/", "/").strip('"\';,')
        full_url = urljoin(base_url, raw_url)
        if is_pdf_url(full_url) and is_allowed_humana_url(full_url):
            ctx = strip_tags(unescaped[max(0, m.start() - 250) : m.end() + 250])
            d, r = best_date(ctx)
            # Find closest text title
            title_m = re.search(r'title["\']?\s*:\s*["\']([^"\']+)["\']', ctx, re.I)
            t = title_m.group(1) if title_m else ""
            pdf_count += reg.add(
                full_url, t, d, r, "embedded_json" if d else "", method, base_url, source_meta
            )

    return pdf_count, sub_pages


def walk_json(node, reg: Registry, method: str, ctx=None, depth=0, base_url=ORIGIN, source_meta=None) -> int:
    ctx = ctx or {}
    source_meta = source_meta or {}
    if depth > 40:
        return 0
    n = 0
    if isinstance(node, dict):
        low = {str(k).lower(): v for k, v in node.items()}
        title, tdepth = ctx.get("title", ""), ctx.get("tdepth", 0) + 1
        for k in TITLE_KEYS:
            v = low.get(k)
            if isinstance(v, str) and clean(v) and not is_pdf_url(v):
                title, tdepth = clean(v), 0
                break
        if tdepth > 2:
            title = ""
        d, drank, dsrc = ctx.get("date"), ctx.get("drank", 0), ctx.get("dsrc", "")
        for k, v in node.items():
            if DATE_KEY_RE.search(str(k)) and isinstance(v, (str, int, float)):
                iso = coerce_date(v)
                r = key_rank(str(k))
                if iso and (r > drank or (r == drank and (not d or iso > d))):
                    d, drank, dsrc = iso, r, f"json:{k}"
        here = {"title": title, "tdepth": tdepth, "date": d, "drank": drank, "dsrc": dsrc}
        for v in node.values():
            if isinstance(v, str):
                if is_pdf_url(v):
                    n += reg.add(v, title, d, drank, dsrc, method, base_url, source_meta)
                elif "<a" in v and "href" in v:
                    cnt, _ = extract_links_from_content(v, reg, method, base_url, source_meta)
                    n += cnt
        for v in node.values():
            if isinstance(v, (dict, list)):
                n += walk_json(v, reg, method, here, depth + 1, base_url, source_meta)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, str) and is_pdf_url(v):
                n += reg.add(
                    v,
                    ctx.get("title", ""),
                    ctx.get("date"),
                    ctx.get("drank", 0),
                    ctx.get("dsrc", ""),
                    method,
                    base_url,
                    source_meta,
                )
            elif isinstance(v, (dict, list)):
                n += walk_json(v, reg, method, ctx, depth + 1, base_url, source_meta)
    elif isinstance(node, str) and is_pdf_url(node):
        n += reg.add(
            node,
            ctx.get("title", ""),
            ctx.get("date"),
            ctx.get("drank", 0),
            ctx.get("dsrc", ""),
            method,
            base_url,
            source_meta,
        )
    return n


# ----------------------------------------------------------------------------
# Browser Automation (Playwright)
# ----------------------------------------------------------------------------
DOM_JS = r"""
() => {
  const PDF = /(pdf(\?.*)?$)|\/is\/content\/|dctm\.humana\.com|chronicleid|\/mentor\/web\//i;
  const isPdfVal = v => v && v.length < 2500 && !/\s/.test(v.trim()) && /^(https?:\/\/|\/)/.test(v) && PDF.test(v);
  const pdfCount = el => [...el.querySelectorAll('*')].filter(x =>
      [...x.attributes].some(a => isPdfVal(a.value))).length;
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    let nlHref = null;
    if (el.tagName && el.tagName.toLowerCase() === 'nucleus-link') {
      nlHref = el.href || (el.shadowRoot && el.shadowRoot.querySelector('a') ? el.shadowRoot.querySelector('a').getAttribute('href') : null);
    }
    if (nlHref && isPdfVal(nlHref)) {
      let c = el, depth = 0;
      while (c.parentElement && c.parentElement !== document.body && depth < 6) { c = c.parentElement; depth++; }
      out.push({href: nlHref,
                text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 300),
                ctx: (c.innerText || '').trim().slice(0, 800)});
      continue;
    }
    for (const attr of el.attributes) {
      if (!isPdfVal(attr.value)) continue;
      let c = el, depth = 0;
      while (c.parentElement && c.parentElement !== document.body && depth < 6 &&
             pdfCount(c.parentElement) <= 1) { c = c.parentElement; depth++; }
      out.push({href: attr.value,
                text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 300),
                ctx: (c.innerText || '').trim().slice(0, 800)});
    }
  }
  return out;
}
"""

MORE_SELECTORS = [
    "button:has-text('Load more')",
    "button:has-text('Show more')",
    "button:has-text('View more')",
    "button:has-text('See more')",
    "a:has-text('Load more')",
    "a:has-text('Show more')",
    "[aria-label*='Next' i]:not([aria-disabled='true'])",
    "button:has-text('Next')",
    "a[rel='next']",
    "li.next a",
    "li.next button",
]
PAGINATION_ITEMS = (
    "[class*='pagination' i] a, [class*='pagination' i] button, "
    "nav[aria-label*='agination' i] a, nav[aria-label*='agination' i] button"
)


def title_from(text: str, ctx: str, url: str) -> str:
    t = clean(text)
    if t and len(t) > 3 and not GENERIC_TITLE.match(t):
        return t
    for line in (ctx or "").splitlines():
        line = clean(line)
        if len(line) > 3 and not GENERIC_TITLE.match(line) and not (find_dates(line) and len(line) < 25):
            return line[:200]
    return ""


def discover_browser_for_source(
    source: dict, reg: Registry, args, out_dir: Path
) -> Tuple[List[dict], List[dict], int]:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []
    debug_dir = out_dir / "debug"
    if args.debug:
        debug_dir.mkdir(exist_ok=True)
    ui_count = 0
    url = source["url"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 2000}, locale="en-US")
        page = ctx.new_page()

        def on_response(resp):
            try:
                rq = resp.request
                u = resp.url
                if rq.resource_type not in ("xhr", "fetch") and not u.split("?")[0].endswith(".json"):
                    return
                if resp.status != 200:
                    return
                body = resp.text()
                if len(body) > 30_000_000:
                    return
                try:
                    data = json.loads(body)
                except ValueError:
                    cnt, _ = extract_links_from_content(body, reg, "xhr-text", u, source)
                    return
                found = walk_json(data, reg, "xhr", base_url=u, source_meta=source)
                if found:
                    captured.append(
                        {
                            "url": u,
                            "method": rq.method,
                            "post": rq.post_data,
                            "found": found,
                            "source_meta": source,
                            "headers": {
                                k: v
                                for k, v in rq.headers.items()
                                if k.lower()
                                in ("content-type", "accept", "authorization", "x-api-key", "referer")
                            },
                        }
                    )
                    log.info("API response with %d PDF links: %s %s", found, rq.method, u[:120])
            except Exception as e:
                log.debug("response handler: %s", e)

        page.on("response", on_response)

        def settle():
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except PWTimeout:
                pass
            page.wait_for_timeout(350)

        def harvest() -> int:
            before = len(reg)
            try:
                rows = page.evaluate(DOM_JS)
            except Exception as e:
                log.debug("DOM harvest failed: %s", e)
                return 0
            for r in rows:
                d, rank = best_date(r["ctx"])
                reg.add(
                    r["href"],
                    title_from(r["text"], r["ctx"], r["href"]),
                    d,
                    rank,
                    "dom:context" if d else "",
                    "dom",
                    page.url,
                    source,
                )
            return len(reg) - before

        def click_first(selector: str) -> bool:
            loc = page.locator(selector)
            try:
                for j in range(min(loc.count(), 5)):
                    el = loc.nth(j)
                    if (
                        el.is_visible()
                        and el.is_enabled()
                        and el.get_attribute("aria-disabled") != "true"
                        and el.get_attribute("disabled") is None
                    ):
                        el.scroll_into_view_if_needed(timeout=2000)
                        el.click(timeout=5000)
                        return True
            except Exception:
                pass
            return False

        def expand(max_rounds=200):
            stable = 0
            for _ in range(max_rounds):
                before = len(reg)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                acted = any(click_first(sel) for sel in MORE_SELECTORS)
                settle()
                harvest()
                stable = stable + 1 if len(reg) == before else 0
                if stable >= 3 or (not acted and stable >= 2):
                    break
            # Check numbered pagination
            try:
                labels = page.locator(PAGINATION_ITEMS).evaluate_all(
                    "els => els.map(e => (e.innerText||'').trim()).filter(t => /^\\d+$/.test(t))"
                )
                for lab in dict.fromkeys(labels):
                    if click_first(f"{PAGINATION_ITEMS.split(',')[0]}:text-is('{lab}')") or click_first(
                        f"nav[aria-label*='agination' i] >> text='{lab}'"
                    ):
                        settle()
                        harvest()
            except Exception as e:
                log.debug("numbered pagination: %s", e)

        log.info("Rendering browser for source: %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            settle()
            for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept')", "button:has-text('Got it')"):
                click_first(sel)
            harvest()
            click_first("button:has-text('Show All')") or click_first("a:has-text('Show All')")
            settle()
            harvest()
            expand()

            # Also extract from page HTML directly
            content = page.content()
            extract_links_from_content(content, reg, "browser-html", url, source)

            body_text = page.inner_text("body")
            counts = [
                int(x.replace(",", ""))
                for x in re.findall(
                    r"(\d[\d,]*)\s+(?:results?|policies|documents|items)", body_text, re.I
                )
            ]
            ui_count = max(counts) if counts else 0
        except Exception as e:
            log.warning("Browser rendering failed for %s: %s", url, e)

        cookies = ctx.cookies()
        browser.close()

    return captured, cookies, ui_count


# ----------------------------------------------------------------------------
# HTTP Replay & Recursive Crawling
# ----------------------------------------------------------------------------
def backoff(attempt: int, retry_after=None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 120)
        except ValueError:
            pass
    return min(60, 2**attempt) + random.uniform(0, 1.0)


def make_session(cookies=None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://provider.humana.com/",
        }
    )
    for c in cookies or []:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def get_with_retry(session, method, url, retries=3, **kw):
    for attempt in range(1, retries + 2):
        try:
            r = session.request(method, url, timeout=(15, 45), **kw)
            if r.status_code in RETRY_STATUS and attempt <= retries:
                time.sleep(backoff(attempt, r.headers.get("Retry-After")))
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt > retries:
                raise
            log.debug("retry %s after %s", url, e)
            time.sleep(backoff(attempt))


def crawl_source_http(
    source: dict, session: requests.Session, reg: Registry, max_depth: int = 2
) -> dict:
    """
    Recursively crawl a source URL and its internal payment sub-pages via HTTP.
    Extracts PDF links and metadata without mixing with coverage policies.
    """
    start_url = source["url"]
    visited = set()
    to_visit = [(start_url, 0)]
    discovered_pdfs = 0
    pages_visited = 0

    log.info("HTTP crawling payment source: %s", start_url)
    while to_visit:
        cur_url, depth = to_visit.pop(0)
        if cur_url in visited or depth > max_depth:
            continue
        visited.add(cur_url)
        pages_visited += 1

        try:
            r = get_with_retry(session, "GET", cur_url, retries=2)
            if not r or r.status_code != 200:
                continue

            content = r.text
            cnt, sub_links = extract_links_from_content(
                content, reg, "http-crawl", cur_url, source
            )
            discovered_pdfs += cnt

            if depth < max_depth:
                for sl in sub_links:
                    if sl not in visited and is_relevant_payment_link(sl):
                        to_visit.append((sl, depth + 1))
        except Exception as e:
            log.warning("HTTP crawl failed on %s: %s", cur_url, e)

    return {
        "source_url": start_url,
        "category": source.get("category"),
        "pages_visited": pages_visited,
        "pdfs_found": discovered_pdfs,
    }


# ----------------------------------------------------------------------------
# PDF Validation & Text Extraction
# ----------------------------------------------------------------------------
def validate_pdf(path: Path, min_bytes: int = 1024):
    size = path.stat().st_size
    if size < min_bytes:
        return False, 0, f"too small ({size} bytes)"
    with open(path, "rb") as f:
        head = f.read(1024)
        if b"%PDF-" not in head:
            return False, 0, "missing %PDF header (likely HTML/error page)"
        f.seek(max(0, size - 8192))
        if b"%%EOF" not in f.read():
            return False, 0, "missing %%EOF trailer (truncated download)"
    try:
        r = PdfReader(str(path), strict=False)
        if r.is_encrypted:
            try:
                if not r.decrypt(""):
                    return True, 0, "encrypted; header/trailer OK but pages not readable"
            except Exception:
                return True, 0, "encrypted; header/trailer OK but pages not readable"
        n = len(r.pages)
        if n < 1:
            return False, 0, "zero pages"
        _ = r.pages[0].mediabox
        return True, n, ""
    except Exception as e:
        return False, 0, f"pypdf could not parse: {e}"


def pdf_date(path: Path):
    try:
        r = PdfReader(str(path), strict=False)
        if r.is_encrypted and not r.decrypt(""):
            return None, ""
        text = " ".join((r.pages[i].extract_text() or "") for i in range(min(2, len(r.pages))))
        iso, _ = best_date(clean(text), labeled_only=True)
        if iso:
            return iso, "pdf_text"
        md = r.metadata
        for dt in (getattr(md, "modification_date", None), getattr(md, "creation_date", None)):
            if dt:
                return dt.date().isoformat(), "pdf_metadata"
    except Exception as e:
        log.debug("pdf_date %s: %s", path, e)
    return None, ""


# ----------------------------------------------------------------------------
# Target CPT Code & Rule Scanner
# ----------------------------------------------------------------------------
RANGE_RE = re.compile(r"\b(15\d{3})\s*[-–—−to/through\s]+\s*(15\d{3})\b", re.I)
RULE_RE = re.compile(r"Rule\s+number\s+([A-Z0-9]+)", re.I)


def scan_pdf_for_target_codes(
    pdf_path: Path, target_codes: Set[str] = TARGET_CPT_CODES
) -> List[dict]:
    """
    Search downloaded PDF text for target CPT codes:
    Supports exact code match AND range expansion (e.g. 15271-15278).
    Captures Rule number (e.g., SC174), page number, and surrounding snippet.
    """
    if not pdf_path.exists():
        return []

    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except Exception as e:
        log.error("Could not read PDF for CPT scan %s: %s", pdf_path, e)
        return []

    results = []
    for pg_idx, page in enumerate(reader.pages):
        pg_num = pg_idx + 1
        try:
            text = page.extract_text() or ""
        except Exception:
            continue

        matched_codes = set()
        match_type = []
        range_match_str = ""

        # 1. Exact matches
        for code in target_codes:
            if re.search(rf"\b{code}\b", text):
                matched_codes.add(code)
                match_type.append(f"exact:{code}")

        # 2. Range matches (e.g. 15271 - 15278)
        for rm in RANGE_RE.finditer(text):
            start_c, end_c = int(rm.group(1)), int(rm.group(2))
            matched_in_range = []
            for code in target_codes:
                if start_c <= int(code) <= end_c:
                    matched_codes.add(code)
                    matched_in_range.append(code)
            if matched_in_range:
                range_str = f"{start_c}–{end_c}"
                range_match_str = range_str
                match_type.append(f"range:{range_str}({','.join(matched_in_range)})")

        if matched_codes:
            # Extract detected rule identifiers (e.g. SC174)
            detected_rules = RULE_RE.findall(text)
            rule_id = detected_rules[-1] if detected_rules else ""

            # Extract surrounding snippet
            snippet = ""
            for code in matched_codes:
                pos = text.find(code)
                if pos != -1:
                    s_idx = max(0, pos - 150)
                    e_idx = min(len(text), pos + 350)
                    snippet = clean(text[s_idx:e_idx])
                    break
            if not snippet and range_match_str:
                m_rg = RANGE_RE.search(text)
                if m_rg:
                    s_idx = max(0, m_rg.start() - 150)
                    e_idx = min(len(text), m_rg.end() + 350)
                    snippet = clean(text[s_idx:e_idx])

            results.append(
                {
                    "page": pg_num,
                    "target_codes": sorted(matched_codes),
                    "match_type": "; ".join(match_type),
                    "detected_rule": rule_id,
                    "all_rules_on_page": detected_rules,
                    "snippet": snippet,
                }
            )

    return results


# ----------------------------------------------------------------------------
# Download Worker & File Processing
# ----------------------------------------------------------------------------
class Retryable(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class Permanent(Exception):
    pass


@dataclass
class Result:
    status: str
    reason: str = ""
    error: str = ""
    http_status: Optional[int] = None
    attempts: int = 0
    record: Optional[dict] = None
    note: str = ""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process(cand: Candidate, rec: Optional[dict], session, dirs: dict, args) -> Result:
    time.sleep(random.uniform(0.1, 0.4))

    # Enforce LOB filter: only Medicare or Medicare+Medicaid dual policies
    target_lob = getattr(args, "lob", "all")
    if target_lob != "all":
        eligible, lob_reason = is_lob_eligible(cand.lob, cand.title, cand.url, target_lob=target_lob)
        if not eligible:
            log.info("SKIPPED DOWNLOAD (LOB filter: %s): %s [%s]", lob_reason, cand.title or cand.url, cand.lob)
            return Result("skipped_lob_filtered", lob_reason, note=f"Excluded from download: {lob_reason} (LOB: {cand.lob})")

    final = dirs["pdfs"] / safe_filename(cand.url, cand.title)
    cond, reason = {}, "new" if not rec else ""

    if rec and not args.force:
        if not Path(rec.get("path", "")).exists():
            reason = "file_missing"
        elif (
            cand.date
            and rec.get("date")
            and cand.date != rec["date"]
            and rec.get("date_source", "").startswith(("json", "dom", "html", "listing"))
        ):
            reason = "listing_date_changed"
        else:
            if rec.get("etag"):
                cond["If-None-Match"] = rec["etag"]
            if rec.get("last_modified"):
                cond["If-Modified-Since"] = rec["last_modified"]
            if not cond and not args.verify_all:
                try:
                    h = get_with_retry(session, "HEAD", cand.url, retries=1, allow_redirects=True)
                    cl = h.headers.get("Content-Length")
                    if h.ok and cl and cl.isdigit() and int(cl) != rec.get("size"):
                        reason = "size_changed"
                    else:
                        return Result("skipped_unchanged", "no_validators_metadata_unchanged", http_status=h.status_code)
                except requests.RequestException:
                    return Result("skipped_unchanged", "head_failed_metadata_unchanged")
            reason = reason or "conditional_check"
    elif args.force:
        reason = "forced"

    max_attempts = args.retries + 1
    last_err, last_status = "", None
    for attempt in range(1, max_attempts + 1):
        tmp = dirs["tmp"] / f"{uuid.uuid4().hex}.part"
        try:
            hdrs = cond if attempt == 1 else {}
            with session.get(cand.url, stream=True, timeout=(15, 60), headers=hdrs, allow_redirects=True) as r:
                last_status = r.status_code
                if r.status_code == 304:
                    return Result("skipped_unchanged", "not_modified_304", http_status=304, attempts=attempt)
                if r.status_code in RETRY_STATUS:
                    raise Retryable(f"HTTP {r.status_code}", r.headers.get("Retry-After"))
                if r.status_code != 200:
                    raise Permanent(f"HTTP {r.status_code}")
                ctype = r.headers.get("Content-Type", "").lower()
                if "html" in ctype:
                    raise Retryable(f"unexpected content-type {ctype} (interstitial/error page?)")
                h, size = hashlib.sha256(), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise Permanent("file exceeds size cap")
                        h.update(chunk)
                        f.write(chunk)
                expected = r.headers.get("Content-Length")
                if expected and expected.isdigit() and not r.headers.get("Content-Encoding") and int(expected) != size:
                    raise Retryable(f"short read ({size}/{expected} bytes)")
                etag, lastmod = r.headers.get("ETag", ""), r.headers.get("Last-Modified", "")

            ok, pages, msg = validate_pdf(tmp)
            if not ok:
                raise Retryable(f"validation failed: {msg}")

            digest = h.hexdigest()
            if rec and rec.get("sha256") == digest and Path(rec.get("path", "")).exists() and not args.force:
                tmp.unlink(missing_ok=True)
                new = dict(rec, etag=etag or rec.get("etag", ""), last_modified=lastmod or rec.get("last_modified", ""))
                return Result("skipped_unchanged", "content_identical", http_status=200, attempts=attempt, record=new)

            status = "updated" if rec else "downloaded"
            if final.exists():
                old = sha256_file(final)[:8]
                shutil.move(str(final), str(dirs["archive"] / f"{final.stem}.{old}.pdf"))
            os.replace(tmp, final)

            dt, dsrc, drank = cand.date, cand.date_source, cand.date_rank
            if not dt:
                dt, dsrc = pdf_date(final)
            if not dt and lastmod:
                try:
                    dt = datetime.strptime(lastmod, "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
                    dsrc = "http_last_modified"
                except ValueError:
                    pass

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            validity_status = (
                "Historical / Informational - Requires Verification"
                if "south_carolina" in cand.url.lower() or "sc" in cand.title.lower()
                else "Active / Current"
            )

            record = {
                "payer": "Humana",
                "url": cand.url,
                "title": cand.title,
                "source_url": cand.source_url,
                "category": cand.category,
                "subtype": cand.subtype,
                "jurisdiction": cand.jurisdiction,
                "line_of_business": cand.lob,
                "date": dt or "",
                "date_source": dsrc or "",
                "path": str(final),
                "sha256": digest,
                "size": size,
                "pages": pages,
                "etag": etag,
                "last_modified": lastmod,
                "first_seen": (rec or {}).get("first_seen", now),
                "last_downloaded": now,
                "last_checked": now,
                "validity_status": validity_status,
            }
            return Result(status, reason or "new", http_status=200, attempts=attempt, record=record, note=msg)
        except Permanent as e:
            tmp.unlink(missing_ok=True)
            return Result("failed", error=str(e), http_status=last_status, attempts=attempt)
        except Retryable as e:
            last_err = str(e)
            tmp.unlink(missing_ok=True)
            if attempt < max_attempts:
                time.sleep(backoff(attempt, e.retry_after))
        except (requests.RequestException, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            tmp.unlink(missing_ok=True)
            if attempt < max_attempts:
                time.sleep(backoff(attempt))
    return Result("failed", error=last_err or "unknown error", http_status=last_status, attempts=max_attempts)


# ----------------------------------------------------------------------------
# State Persistence
# ----------------------------------------------------------------------------
def load_state(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            shutil.copy(p, p.with_suffix(".corrupt.json"))
            log.error("state.json was corrupt; backed up and starting fresh")
    return {"version": 2, "files": {}, "last_discovery_count": 0, "runs": []}


def save_state(p: Path, state: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def clean_excluded_files(dirs: dict, state: dict, target_lob: str = "medicare") -> int:
    """Move any previously downloaded PDFs that do not meet the target LOB to the archive directory."""
    archive_dir = dirs["archive"]
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved_count = 0

    for key, rec in list(state.get("files", {}).items()):
        path_str = rec.get("path")
        if not path_str:
            continue
        p = Path(path_str)
        lob = rec.get("line_of_business", "")
        title = rec.get("title", "")
        url = rec.get("url", "")
        eligible, reason = is_lob_eligible(lob, title, url, target_lob=target_lob)
        if not eligible and p.exists() and p.resolve().parent == dirs["pdfs"].resolve():
            dest = archive_dir / p.name
            log.info("Archiving excluded Medicaid PDF: %s -> %s", p.name, dest)
            try:
                shutil.move(str(p), str(dest))
                rec["path"] = str(dest)
                rec["validity_status"] = "Excluded - Medicaid Only (Archived)"
                moved_count += 1
            except Exception as e:
                log.warning("Could not move %s: %s", p, e)

    # Check for any stray Medicaid PDFs in pdfs directory
    for pdf_file in dirs["pdfs"].glob("*.pdf"):
        fname_lower = pdf_file.name.lower()
        if any(k in fname_lower for k in ("south_carolina", "sc_prov", "pathways", "ratings", "consolidated_g", "epsdt")):
            dest = archive_dir / pdf_file.name
            log.info("Archiving stray Medicaid PDF: %s -> %s", pdf_file.name, dest)
            try:
                shutil.move(str(pdf_file), str(dest))
                moved_count += 1
            except Exception as e:
                log.warning("Could not move %s: %s", pdf_file, e)

    return moved_count


# ----------------------------------------------------------------------------
# Main Execution Pipeline
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="humana_policies")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--crawl-depth", type=int, default=2, help="Recursive crawl depth for payment links")
    ap.add_argument("--exhaustive", action="store_true", help="cycle select filters & search queries")
    ap.add_argument("--force", action="store_true", help="re-download all discovered documents")
    ap.add_argument("--verify-all", action="store_true", help="re-fetch to compare byte hashes")
    ap.add_argument("--discover-only", action="store_true", help="discover links without downloading")
    ap.add_argument("--no-browser", action="store_true", help="skip Playwright rendering")
    ap.add_argument("--headed", action="store_true", help="run Playwright with visible browser")
    ap.add_argument("--debug", action="store_true", help="save debug dumps and screenshots")
    ap.add_argument("--min-expected", type=int, default=0, help="exit code 2 if fewer PDFs discovered")
    ap.add_argument(
        "--lob",
        default="medicare",
        choices=["medicare", "all"],
        help="Line of business filter: 'medicare' (downloads Medicare and Medicare+Medicaid dual policies; strictly excludes Medicaid-only) or 'all' (default: medicare)",
    )
    ap.add_argument(
        "--clean-excluded",
        action="store_true",
        help="Move any existing downloaded PDFs that do not match target LOB to archive",
    )
    args = ap.parse_args()

    out = Path(args.out).resolve()
    dirs = {"pdfs": out / "pdfs", "archive": out / "archive", "tmp": out / "tmp"}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(out / "scraper.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    lock = out / ".lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        log.error("Another run appears active (%s). Delete the file if it is stale.", lock)
        return 4
    try:
        return run(args, out, dirs)
    finally:
        lock.unlink(missing_ok=True)
        shutil.rmtree(dirs["tmp"], ignore_errors=True)


def run(args, out: Path, dirs: dict) -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state_path = out / "state.json"
    state = load_state(state_path)
    reg = Registry()
    source_audit_report = []

    # =========================================================================
    # Phase 1: Multi-Source Discovery
    # =========================================================================
    session = make_session()

    # Filter sources if running in medicare-only mode
    sources_to_crawl = []
    for src in PAYMENT_SOURCES:
        if args.lob == "medicare":
            eligible, reason = is_lob_eligible(src.get("lob", ""), src.get("subtype", ""), src.get("url", ""), target_lob="medicare")
            if not eligible:
                log.info("Skipping discovery source '%s' (LOB: %s) - pure Medicaid source excluded for Medicare pipeline", src["url"], src.get("lob"))
                continue
        sources_to_crawl.append(src)

    log.info("Starting Multi-Source Payment Policy Discovery across %d sources (target LOB: %s)", len(sources_to_crawl), args.lob)

    for src in sources_to_crawl:
        url = src["url"]
        log.info("--- Discovery Source: %s (%s) ---", url, src["category"])
        src_status = {"url": url, "category": src["category"], "browser_rendered": False, "status": "ok"}

        # A. Browser rendering (if requested and browser not disabled)
        if src.get("render_browser") and not args.no_browser:
            try:
                captured, cookies, ui_count = discover_browser_for_source(src, reg, args, out)
                src_status["browser_rendered"] = True
                src_status["ui_count"] = ui_count
                if cookies:
                    session = make_session(cookies)
            except Exception as e:
                log.warning("Browser discovery skipped/failed for %s: %s", url, e)
                src_status["browser_error"] = str(e)

        # B. HTTP recursive crawl across payment sub-links & Content Fragments
        try:
            crawl_stats = crawl_source_http(src, session, reg, max_depth=args.crawl_depth)
            src_status.update(crawl_stats)
        except Exception as e:
            log.error("HTTP crawl failed for %s: %s", url, e)
            src_status["http_error"] = str(e)
            src_status["status"] = "error"

        source_audit_report.append(src_status)

    n = len(reg)
    log.info("=" * 60)
    log.info("Total unique PDF candidate documents discovered across all sources: %d", n)

    if n == 0 or n < args.min_expected:
        log.error("Discovery found %d PDFs (expected >= %d). Aborting.", n, max(args.min_expected, 1))
        return 2

    # Save discovery provenance report
    (out / "source_discovery_report.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "total_discovered_pdfs": n,
                "sources_crawled": source_audit_report,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # =========================================================================
    # Phase 2: Download / Skip & Metadata Recording
    # =========================================================================
    cands = sorted(reg.items.values(), key=lambda c: c.url)
    results: dict[str, Result] = {}

    # LOB candidate filtering: strictly download Medicare or Medicare+Medicaid dual policies
    eligible_cands = []
    excluded_count = 0
    for c in cands:
        eligible, reason = is_lob_eligible(c.lob, c.title, c.url, target_lob=args.lob)
        if not eligible:
            log.info("SKIPPED (LOB Filter: %s) %s -> %s", reason, c.title or c.url, c.lob)
            results[c.key] = Result(
                "skipped_lob_filtered",
                reason,
                note=f"Excluded from download: {reason} (LOB: {c.lob})",
            )
            excluded_count += 1
        else:
            eligible_cands.append(c)

    log.info(
        "LOB Filter summary: %d candidates eligible for download (Medicare/Dual), %d excluded (pure Medicaid)",
        len(eligible_cands),
        excluded_count,
    )

    # Clean existing excluded PDFs from pdfs directory if requested
    if getattr(args, "clean_excluded", False):
        cleaned = clean_excluded_files(dirs, state, target_lob=args.lob)
        if cleaned > 0:
            log.info("Cleaned/archived %d existing excluded files from pdfs directory", cleaned)
            save_state(state_path, state)

    if args.discover_only:
        for c in eligible_cands:
            results[c.key] = Result("discovered_only")
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            fut = {
                ex.submit(process, c, state["files"].get(c.key), session, dirs, args): c
                for c in eligible_cands
            }
            for f in as_completed(fut):
                c = fut[f]
                try:
                    res = f.result()
                except Exception as e:
                    log.exception("worker crashed for %s", c.url)
                    res = Result("failed", error=f"internal error: {e}")
                results[c.key] = res
                rec = state["files"].get(c.key)
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if res.record:
                    state["files"][c.key] = res.record
                elif rec and res.status == "skipped_unchanged":
                    rec["last_checked"] = now
                    if c.date and not rec.get("date"):
                        rec["date"], rec["date_source"] = c.date, c.date_source
                if res.status == "failed":
                    log.error("FAILED %s -> %s", c.url, res.error)
                else:
                    log.info("%-17s %s", res.status, c.title[:70] or c.url[-70:])
                done += 1
                if done % 25 == 0:
                    save_state(state_path, state)

    # =========================================================================
    # Phase 3: Group Versions & Identify Latest
    # =========================================================================
    groups: dict[str, list] = {}
    info: dict[str, dict] = {}
    for c in cands:
        rec = state["files"].get(c.key, {})
        title = rec.get("title") or c.title
        d = c.date or rec.get("date", "")
        dsrc = c.date_source if c.date else rec.get("date_source", "")
        pk = policy_key(title, c.url)
        info[c.key] = {
            "title": title,
            "date": d,
            "date_source": dsrc,
            "pk": pk,
            "rec": rec,
            "ver": version_tuple(title, c.url),
        }
        groups.setdefault(pk, []).append(c.key)

    latest: dict[str, str] = {}
    for pk, keys in groups.items():
        latest[pk] = max(keys, key=lambda k: (info[k]["date"] or "", info[k]["ver"], k))
        for k in keys:
            state["files"].get(k, {})["policy_key"] = pk

    by_hash: dict[str, str] = {}
    dup_of: dict[str, str] = {}
    for c in cands:
        sh = info[c.key]["rec"].get("sha256")
        if sh:
            if sh in by_hash:
                dup_of[c.key] = by_hash[sh]
            else:
                by_hash[sh] = c.url

    # =========================================================================
    # Phase 4: Target CPT Code & Rule SC174 Scanning
    # =========================================================================
    log.info("Scanning all downloaded PDFs for target CPT codes %s...", sorted(TARGET_CPT_CODES))
    cpt_match_records = []
    cpt_summary_rows = []

    for c in cands:
        rec = state["files"].get(c.key, {})
        pdf_path_str = rec.get("path")
        if not pdf_path_str or not Path(pdf_path_str).exists():
            continue

        pdf_path = Path(pdf_path_str)
        matches = scan_pdf_for_target_codes(pdf_path, TARGET_CPT_CODES)
        if matches:
            matched_cpt_set = set()
            matched_pages = []
            detected_rules = []

            for m in matches:
                matched_cpt_set.update(m["target_codes"])
                matched_pages.append(m["page"])
                if m.get("detected_rule"):
                    detected_rules.append(m["detected_rule"])

                # Detailed match row
                cpt_summary_rows.append(
                    {
                        "payer": "Humana",
                        "policy_title": rec.get("title") or c.title,
                        "document_url": c.url,
                        "source_page_url": c.source_url,
                        "local_file": pdf_path.name,
                        "category": c.category,
                        "jurisdiction": c.jurisdiction,
                        "line_of_business": c.lob,
                        "page_number": m["page"],
                        "matched_cpt_codes": ",".join(m["target_codes"]),
                        "match_type": m["match_type"],
                        "rule_identifier": m.get("detected_rule", ""),
                        "surrounding_rule_text": m["snippet"],
                        "validity_status": rec.get(
                            "validity_status", "Historical / Informational - Requires Verification"
                        ),
                    }
                )

            rec["target_cpt_codes"] = ",".join(sorted(matched_cpt_set))
            rec["matched_pages"] = ",".join(str(p) for p in sorted(set(matched_pages)))
            rec["detected_rules"] = ",".join(sorted(set(detected_rules)))

            cpt_match_records.append(
                {
                    "title": rec.get("title") or c.title,
                    "url": c.url,
                    "source_url": c.source_url,
                    "category": c.category,
                    "jurisdiction": c.jurisdiction,
                    "target_codes": sorted(matched_cpt_set),
                    "matched_pages": sorted(set(matched_pages)),
                    "detected_rules": sorted(set(detected_rules)),
                    "findings": matches,
                }
            )

    log.info("Found %d payment documents matching target CPT codes", len(cpt_match_records))

    # Write target CPT match outputs
    cpt_json_path = out / "target_cpt_matches.json"
    cpt_json_path.write_text(json.dumps(cpt_match_records, indent=2), encoding="utf-8")
    log.info("Wrote target CPT detailed matches to %s", cpt_json_path)

    cpt_csv_path = out / "target_cpt_matches.csv"
    if cpt_summary_rows:
        fieldnames = list(cpt_summary_rows[0].keys())
        with open(cpt_csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(cpt_summary_rows)
        log.info("Wrote target CPT CSV summary to %s", cpt_csv_path)

    # =========================================================================
    # Phase 5: Export Audit & Latest Policies Reports
    # =========================================================================
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = [
        "run_id",
        "timestamp_utc",
        "payer",
        "policy_key",
        "title",
        "url",
        "source_page_url",
        "category",
        "subtype",
        "jurisdiction",
        "line_of_business",
        "publication_date",
        "effective_date",
        "expiration_date",
        "validity_status",
        "target_cpt_codes",
        "matched_pages",
        "detected_rules",
        "discovery_methods",
        "times_seen",
        "status",
        "reason",
        "error",
        "http_status",
        "attempts",
        "local_path",
        "size_bytes",
        "pages",
        "sha256",
        "is_latest_version",
        "latest_version_url",
        "versions_in_group",
        "duplicate_content_of",
        "notes",
    ]

    counts: dict[str, int] = {}
    audit_path = out / f"audit_{run_id}.csv"
    with open(audit_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for c in cands:
            i, res = info[c.key], results[c.key]
            rec = i["rec"] or {}
            counts[res.status] = counts.get(res.status, 0) + 1
            lk = latest[i["pk"]]
            w.writerow(
                {
                    "run_id": run_id,
                    "timestamp_utc": now,
                    "payer": "Humana",
                    "policy_key": i["pk"],
                    "title": i["title"],
                    "url": c.url,
                    "source_page_url": c.source_url,
                    "category": c.category,
                    "subtype": c.subtype,
                    "jurisdiction": c.jurisdiction,
                    "line_of_business": c.lob,
                    "publication_date": i["date"],
                    "effective_date": rec.get("effective_date", "Unknown"),
                    "expiration_date": rec.get("expiration_date", "Unknown"),
                    "validity_status": rec.get("validity_status", "Active / Current"),
                    "target_cpt_codes": rec.get("target_cpt_codes", ""),
                    "matched_pages": rec.get("matched_pages", ""),
                    "detected_rules": rec.get("detected_rules", ""),
                    "discovery_methods": "|".join(sorted(c.methods)),
                    "times_seen": c.seen,
                    "status": res.status,
                    "reason": res.reason,
                    "error": res.error,
                    "http_status": res.http_status or "",
                    "attempts": res.attempts,
                    "local_path": rec.get("path", ""),
                    "size_bytes": rec.get("size", ""),
                    "pages": rec.get("pages", ""),
                    "sha256": rec.get("sha256", ""),
                    "is_latest_version": c.key == lk,
                    "latest_version_url": reg.items[lk].url,
                    "versions_in_group": len(groups[i["pk"]]),
                    "duplicate_content_of": dup_of.get(c.key, ""),
                    "notes": res.note,
                }
            )

    with open(out / "latest_policies.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "policy_key",
                "title",
                "category",
                "jurisdiction",
                "line_of_business",
                "publication_date",
                "target_cpt_codes",
                "url",
                "local_path",
                "versions_found",
            ]
        )
        for pk, k in sorted(latest.items()):
            cand_obj = reg.items[k]
            # When filtering by LOB, latest_policies.csv should reflect active pipeline policies
            if args.lob == "medicare" and not is_lob_eligible(cand_obj.lob, cand_obj.title, cand_obj.url, target_lob="medicare")[0]:
                continue
            rec = info[k]["rec"] or {}
            w.writerow(
                [
                    pk,
                    info[k]["title"],
                    cand_obj.category,
                    cand_obj.jurisdiction,
                    cand_obj.lob,
                    info[k]["date"],
                    rec.get("target_cpt_codes", ""),
                    cand_obj.url,
                    rec.get("path", ""),
                    len(groups[pk]),
                ]
            )

    if not args.discover_only:
        state["last_discovery_count"] = n
        state["runs"] = (
            state.get("runs", []) + [{"run_id": run_id, "discovered": n, "counts": counts}]
        )[-50:]
        save_state(state_path, state)

    multi = sum(1 for v in groups.values() if len(v) > 1)
    log.info("=" * 60)
    log.info(
        "Discovered %d | policies %d (%d multi-version) | %s",
        n,
        len(groups),
        multi,
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
    )
    log.info("Audit file: %s", audit_path)
    log.info("Latest policies: %s", out / "latest_policies.csv")
    if counts.get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
