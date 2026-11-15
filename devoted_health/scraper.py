#!/usr/bin/env python3
"""
Devoted Health Medicare Advantage Payment Policy Scraper
=========================================================

Extracts and downloads publicly accessible payment policy documents
from the official Devoted Health provider website:
  https://www.devoted.com/providers/payment-policies/

Also inspects relevant provider compliance/claims policy pages for
additional publicly accessible payment and claims policy documents.

Directory Structure:
  devoted_health/
  ├── scraper.py
  └── policies/
      ├── Acute_Admissions_and_Level_of_Care.pdf
      ├── Ambulatory_Surgical_Centers.pdf
      ├── Common_Modifiers.pdf
      └── ...
"""

import hashlib
import io
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

PRIMARY_SOURCE_URL = "https://www.devoted.com/providers/payment-policies/"
ADDITIONAL_SOURCE_URLS = [
    "https://www.devoted.com/providers/providers-policies-compliance/",
]

# Words kept lowercase in title-cased filenames (unless first word)
LOWERCASE_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"
}

# Recognized acronyms to preserve exact uppercase casing
PRESERVE_ACRONYMS = {
    "ABNs", "GY", "PCP", "IDE", "CED", "CGMs", "NDCs", "CAH", "FQHC",
    "DRG", "NOMNC", "TIN", "Q6", "KX", "PPO", "HMO", "CMS", "CPT",
    "HCPCS", "NDC", "POS", "JW", "JZ", "MA", "DSNP"
}

REQUEST_DELAY_SEC = 0.35  # Polite delay between document downloads

# Set up module logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("devoted_scraper")


# ---------------------------------------------------------------------------
# 1. Directory Setup
# ---------------------------------------------------------------------------
def create_directories(base_dir: Optional[Path] = None) -> Path:
    """
    Ensure the output directories exist.
    Defaults to devoted_health/policies relative to this script.
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent

    policies_dir = base_dir / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    return policies_dir


# ---------------------------------------------------------------------------
# 2. URL Normalization
# ---------------------------------------------------------------------------
def normalize_url(url: str, base_url: str = PRIMARY_SOURCE_URL) -> str:
    """
    Convert relative URLs to absolute URLs and clean whitespace.
    Preserves exact asset paths (including CloudFront/S3 asset keys).
    """
    url = url.strip()
    if not url:
        return ""
    # Check if relative
    if not url.startswith("http://") and not url.startswith("https://"):
        return urljoin(base_url, url)
    return url


# ---------------------------------------------------------------------------
# 3. Filename Sanitization & Formatting
# ---------------------------------------------------------------------------
def _format_title_casing(title: str) -> str:
    """
    Apply title casing matching standard policy naming conventions:
    e.g. 'Ambulatory surgical centers' -> 'Ambulatory Surgical Centers'
         'Acute Admissions and Level of Care' -> 'Acute Admissions and Level of Care'
    """
    words = title.split()
    formatted = []
    for i, w in enumerate(words):
        clean_w = re.sub(r"[^a-zA-Z0-9]", "", w)
        if clean_w in PRESERVE_ACRONYMS or (clean_w.isupper() and len(clean_w) > 1):
            formatted.append(w)
        elif any(c.isupper() for c in clean_w[1:]) and not clean_w.isupper():
            # Mixed-case acronyms like CGMs, NDCs
            formatted.append(w)
        elif i > 0 and clean_w.lower() in LOWERCASE_WORDS:
            formatted.append(w.lower())
        else:
            formatted.append(w.capitalize())
    return " ".join(formatted)


def clean_filename(title: str, url: str = "", max_length: int = 150) -> str:
    """
    Sanitize policy title into a meaningful, valid filesystem filename.
    - Preserves meaningful policy titles.
    - Formats casing consistently.
    - Replaces '&' with 'and'.
    - Strips characters invalid on Windows/POSIX (<>:"/\\|?*).
    - Prevents excessive length and ensures .pdf extension.
    """
    # Normalize unicode characters
    normalized = unicodedata.normalize("NFKD", title)
    # Replace & with and
    normalized = re.sub(r"\s*&\s*", " and ", normalized)
    # Format casing
    cased = _format_title_casing(normalized)
    # Remove filesystem invalid characters and extra punctuation
    clean = re.sub(r'[<>:"/\\|?*]+', "", cased)
    clean = re.sub(r"[\(\)\[\],;]+", "", clean)
    clean = re.sub(r"[\s\-]+", " ", clean).strip()
    filename = clean.replace(" ", "_")

    if not filename:
        # Fallback if title was entirely symbols
        url_hash = hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:10]
        filename = f"policy_{url_hash}"

    if len(filename) > max_length:
        filename = filename[:max_length].rstrip("_")

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    return filename


# ---------------------------------------------------------------------------
# 4. HTTP Fetching
# ---------------------------------------------------------------------------
def fetch_page(
    url: str,
    session: requests.Session,
    max_retries: int = 3,
    timeout: int = 15,
) -> Optional[str]:
    """
    Fetch an HTML page using the provided requests session with retry handling.
    """
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Fetching webpage: %s (attempt %d/%d)", url, attempt, max_retries)
            response = session.get(url, timeout=timeout)
            if response.status_code == 200:
                return response.text
            logger.warning(
                "HTTP %d returned for %s", response.status_code, url
            )
            if response.status_code in (404, 403, 410):
                # Permanent client error
                return None
        except requests.RequestException as exc:
            logger.warning("Network error fetching %s (attempt %d): %s", url, attempt, exc)
            time.sleep(1.0 * attempt)

    logger.error("Failed to fetch page after %d retries: %s", max_retries, url)
    return None


# ---------------------------------------------------------------------------
# 5. Policy Link Extraction
# ---------------------------------------------------------------------------
def extract_policy_links(html: str, source_url: str) -> List[Dict[str, Any]]:
    """
    Parse HTML and extract metadata for all payment and claims policy document links.
    Filters out navigation, footers, general forms, marketing flyers, and partner guides.
    """
    soup = BeautifulSoup(html, "html.parser")
    policies: List[Dict[str, Any]] = []

    # If parsing the primary payment-policies page:
    # Target the main content section containing the policy catalog
    if "payment-policies" in source_url:
        # Find pageContent container or main element
        content_container = (
            soup.find("div", class_=lambda c: c and "pageLayout-module--pageContent" in c)
            or soup.find("main")
            or soup
        )
        anchors = content_container.find_all("a", href=True)
    else:
        # General provider page
        main_container = soup.find("main") or soup
        anchors = main_container.find_all("a", href=True)

    # Keywords that indicate non-policy documents to skip
    skip_title_keywords = {
        "skip to content", "login", "portal", "find a plan", "apply for a plan",
        "why devoted", "understanding medicare", "search providers", "search prescription",
        "care ondemand", "benefits overview", "plan documents", "member forms",
        "chat with us", "review your rights", "contact us", "privacy policy",
        "terms of use", "disclaimers", "careers", "about us", "our team",
        "service area", "newsroom", "chinese plan materials", "accessibility",
        "nondiscrimination", "quality improvement", "partner guide",
        "tool comparison", "quick reference", "waiver of liability",
        "claims submission guide", "prior authorization request form",
        "regulatory attachment", "state regulatory exhibit", "exhibit provider"
    }

    seen_hrefs: Set[str] = set()

    for a in anchors:
        raw_title = a.get_text(strip=True)
        raw_href = a["href"].strip()

        if not raw_title or not raw_href:
            continue

        normalized_href = normalize_url(raw_href, source_url)
        norm_title_lower = raw_title.lower()

        # Skip anchor links, telephone links, mailto
        if normalized_href.startswith(("tel:", "mailto:", "#", "javascript:")):
            continue

        # Skip known navigation / marketing / general items
        if any(skip_kw in norm_title_lower for skip_kw in skip_title_keywords):
            continue

        # Determine if this link is a payment/claims policy
        is_policy = False
        if "payment-policies" in source_url:
            # All links inside the catalog of payment-policies pointing to assets or docs are policies
            if "assets.devoted.com" in normalized_href or ".pdf" in normalized_href.lower():
                is_policy = True
        else:
            # On secondary pages, strictly look for explicit payment/claims policy documents
            if ("policy" in norm_title_lower or "policy" in normalized_href.lower()) and (
                "pcp assignment" in norm_title_lower or "claims" in norm_title_lower or "payment" in norm_title_lower
            ):
                is_policy = True

        if not is_policy:
            continue

        if normalized_href in seen_hrefs:
            continue
        seen_hrefs.add(normalized_href)

        # Build policy metadata entry
        policies.append({
            "title": raw_title,
            "original_url": normalized_href,
            "source_url": source_url,
            "file_format": "PDF",
            "download_status": "PENDING",
            "filename": clean_filename(raw_title, normalized_href),
            "file_path": None,
            "file_size": 0,
            "error": None,
        })

    logger.info("Extracted %d policy links from %s", len(policies), source_url)
    return policies


# ---------------------------------------------------------------------------
# 6. Policy Document Download & Verification
# ---------------------------------------------------------------------------
def _is_valid_pdf_content(content: bytes) -> bool:
    """Verify bytes represent a non-empty, valid PDF."""
    if len(content) < 100:
        return False
    # PDF magic bytes
    if not content.startswith(b"%PDF"):
        return False
    # Optional structural check with pypdf
    if PYPDF_AVAILABLE:
        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            return len(reader.pages) > 0
        except Exception:
            return False
    return True


def download_policy(
    policy: Dict[str, Any],
    output_dir: Path,
    session: requests.Session,
    max_retries: int = 3,
    timeout: int = 25,
) -> Dict[str, Any]:
    """
    Download a single policy document into output_dir.
    - Detects existing valid files (does not overwrite unnecessarily).
    - Validates HTTP status code.
    - Validates PDF content header and structure.
    - Records progress and status.
    """
    title = policy["title"]
    url = policy["original_url"]
    target_path = output_dir / policy["filename"]

    # Check if file already exists on disk and is a valid PDF
    if target_path.exists() and target_path.stat().st_size > 500:
        try:
            existing_bytes = target_path.read_bytes()
            if _is_valid_pdf_content(existing_bytes):
                policy["download_status"] = "SKIPPED_EXISTING"
                policy["file_path"] = str(target_path)
                policy["file_size"] = len(existing_bytes)
                logger.info("[EXISTING] %s (%d bytes) -> %s", title, len(existing_bytes), target_path.name)
                return policy
        except Exception as e:
            logger.warning("Existing file invalid or unreadable: %s (%s). Re-downloading.", target_path, e)

    # Perform download with retry handling
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Downloading: '%s' (attempt %d/%d)...", title, attempt, max_retries)
            resp = session.get(url, headers=REQUEST_HEADERS, timeout=timeout)

            if resp.status_code != 200:
                raise ValueError(f"HTTP status {resp.status_code} ({resp.reason})")

            content = resp.content
            if not _is_valid_pdf_content(content):
                # Check Content-Type header
                ct = resp.headers.get("Content-Type", "")
                raise ValueError(f"Downloaded content is not a valid PDF (Content-Type: {ct}, len: {len(content)})")

            # Write file to disk
            target_path.write_bytes(content)

            # Final disk check
            if not target_path.exists() or target_path.stat().st_size == 0:
                raise IOError(f"File was not successfully written to disk: {target_path}")

            policy["download_status"] = "DOWNLOADED"
            policy["file_path"] = str(target_path)
            policy["file_size"] = len(content)
            logger.info(
                "[SUCCESS] '%s' (%d bytes) saved as '%s'",
                title, len(content), target_path.name
            )
            time.sleep(REQUEST_DELAY_SEC)
            return policy

        except Exception as exc:
            logger.warning("Attempt %d failed for '%s': %s", attempt, title, exc)
            if attempt < max_retries:
                time.sleep(1.0 * attempt)
            else:
                policy["download_status"] = "FAILED"
                policy["error"] = str(exc)
                logger.error("[FAILED] Could not download '%s' from %s: %s", title, url, exc)

    return policy


# ---------------------------------------------------------------------------
# 7. Orchestration: Scrape Policies
# ---------------------------------------------------------------------------
def scrape_policies(
    output_dir: Optional[Path] = None,
    primary_url: str = PRIMARY_SOURCE_URL,
    additional_urls: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Main orchestration function to discover, deduplicate, and download
    all Devoted Health payment policies.
    """
    if additional_urls is None:
        additional_urls = ADDITIONAL_SOURCE_URLS

    policies_dir = create_directories(output_dir.parent if output_dir else None)
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    logger.info("=" * 80)
    logger.info("DEVOTED HEALTH PAYMENT POLICY SCRAPER")
    logger.info("Primary URL: %s", primary_url)
    logger.info("Target Output Directory: %s", policies_dir)
    logger.info("=" * 80)

    # Step 1: Fetch and extract from primary source
    html_primary = fetch_page(primary_url, session)
    if not html_primary:
        logger.error("Failed to retrieve primary payment policies webpage: %s", primary_url)
        return []

    all_policies: List[Dict[str, Any]] = extract_policy_links(html_primary, primary_url)

    # Step 2: Fetch and extract from additional relevant provider policy pages
    for add_url in additional_urls:
        html_sec = fetch_page(add_url, session)
        if html_sec:
            sec_policies = extract_policy_links(html_sec, add_url)
            all_policies.extend(sec_policies)

    # Step 3: Deduplicate by original URL
    unique_policies: List[Dict[str, Any]] = []
    seen_urls: Set[str] = set()
    skipped_duplicates = 0

    for pol in all_policies:
        u = pol["original_url"]
        if u in seen_urls:
            skipped_duplicates += 1
            logger.info("Skipping duplicate policy URL: %s", u)
            continue
        seen_urls.add(u)
        unique_policies.append(pol)

    logger.info("Discovered %d unique policy documents to download.", len(unique_policies))

    # Step 4: Resolve filename collisions across discovered policies
    seen_filenames: Dict[str, str] = {}
    for pol in unique_policies:
        base_fn = pol["filename"]
        u = pol["original_url"]
        if base_fn in seen_filenames and seen_filenames[base_fn] != u:
            # Collision: append URL hash
            u_hash = hashlib.sha256(u.encode("utf-8")).hexdigest()[:8]
            stem = Path(base_fn).stem
            resolved_fn = f"{stem}_{u_hash}.pdf"
            logger.info("Filename collision for '%s': resolved to '%s'", base_fn, resolved_fn)
            pol["filename"] = resolved_fn
        else:
            seen_filenames[base_fn] = u

    # Step 5: Download each policy document
    results: List[Dict[str, Any]] = []
    for idx, pol in enumerate(unique_policies, 1):
        logger.info("[%d/%d] Processing '%s'...", idx, len(unique_policies), pol["title"])
        res = download_policy(pol, policies_dir, session)
        results.append(res)

    return results


# ---------------------------------------------------------------------------
# 8. Main Entrypoint & Reporting
# ---------------------------------------------------------------------------
def main() -> int:
    """
    Command-line entrypoint.
    Executes the scraper, verifies downloaded documents, and prints the summary.
    """
    script_dir = Path(__file__).resolve().parent
    policies_dir = script_dir / "policies"

    start_time = time.time()
    results = scrape_policies(output_dir=policies_dir)
    elapsed_sec = time.time() - start_time

    # Calculate summary metrics
    total_discovered = len(results)
    downloaded_count = sum(1 for r in results if r["download_status"] == "DOWNLOADED")
    existing_count = sum(1 for r in results if r["download_status"] == "SKIPPED_EXISTING")
    failed_count = sum(1 for r in results if r["download_status"] == "FAILED")
    success_count = downloaded_count + existing_count

    # Print Final Summary Report
    print("\n" + "=" * 90)
    print("DEVOTED HEALTH PAYMENT POLICY DOWNLOAD SUMMARY")
    print("=" * 90)
    print(f"Total Policies Discovered:          {total_discovered:d}")
    print(f"Successfully Downloaded / Verified: {success_count:d} (New: {downloaded_count:d}, Existing: {existing_count:d})")
    print(f"Failed Downloads:                   {failed_count:d}")
    print(f"Storage Directory:                  {policies_dir}")
    print(f"Total Execution Time:               {elapsed_sec:.2f} seconds")
    print("=" * 90)

    # Detailed tabular inventory
    print(f"\n{'#':<3} | {'Status':<16} | {'Size (KB)':<10} | {'Policy Title':<50} | {'Filename'}")
    print("-" * 120)
    for idx, r in enumerate(results, 1):
        status = r["download_status"]
        size_kb = f"{r['file_size'] / 1024:.1f} KB" if r["file_size"] > 0 else "-"
        title_trunc = (r["title"][:47] + "...") if len(r["title"]) > 50 else r["title"]
        print(f"{idx:<3} | {status:<16} | {size_kb:<10} | {title_trunc:<50} | {r['filename']}")

    print("\n" + "=" * 90)
    print("ORIGINAL URL TRACEABILITY")
    print("=" * 90)
    for idx, r in enumerate(results, 1):
        print(f"[{idx:02d}] {r['title']}")
        print(f"     Path: {r['file_path']}")
        print(f"     URL:  {r['original_url']}")
        if r.get("error"):
            print(f"     ERROR: {r['error']}")
    print("=" * 90 + "\n")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

