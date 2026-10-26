#!/usr/bin/env python3


import argparse
import base64
import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import undetected_chromedriver as webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
# from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

PAGE_URL = (
    "https://www.aetnabetterhealth.com/illinois-medicaid/providers/"
    "clinical-payment-policies.html"
)

PAYMENT_TITLE = "payment policies"
UM_TITLE = "utilization management policies"

# --------------------------------------------------------------------------- #
# JavaScript run inside the page
# --------------------------------------------------------------------------- #

# Finds the accordion section whose header text equals `target`, and returns the
# PDF links inside it. Works on hidden/collapsed panels because it reads the DOM.
EXTRACT_SECTION_JS = r"""
const target = arguments[0];
const others = arguments[1];           // titles of sibling sections that must NOT be inside the container
const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
const pdfAnchors = root => [...root.querySelectorAll('a[href]')]
    .filter(a => /\.pdf([?#]|$)/i.test(a.getAttribute('href') || ''));

const headers = [...document.querySelectorAll('*')]
    .filter(e => norm(e.textContent) === target && e.children.length <= 3);

for (const header of headers) {
    let node = header;
    while (node.parentElement && node.parentElement !== document.body) {
        node = node.parentElement;
        const anchors = pdfAnchors(node);
        if (anchors.length === 0) continue;
        const txt = norm(node.textContent);
        if (others.some(o => txt.includes(o))) break;   // too big: spans several sections
        const seen = new Set(), out = [];
        for (const a of anchors) {
            if (seen.has(a.href)) continue;
            seen.add(a.href);
            out.push({href: a.href, text: (a.textContent || '').replace(/\s+/g, ' ').trim()});
        }
        return out;
    }
}
return [];
"""

EXTRACT_ALL_PDFS_JS = r"""
const seen = new Set(), out = [];
for (const a of document.querySelectorAll('a[href]')) {
    if (!/\.pdf([?#]|$)/i.test(a.getAttribute('href') || '')) continue;
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    out.push({href: a.href, text: (a.textContent || '').replace(/\s+/g, ' ').trim()});
}
return out;
"""

# Click any collapsed accordion headers (harmless if already open).
EXPAND_JS = r"""
document.querySelectorAll("[aria-expanded='false']").forEach(el => { try { el.click(); } catch(e) {} });
"""

# Same-origin download via a real link click (keeps cookies + referrer).
CLICK_DOWNLOAD_JS = r"""
const a = document.createElement('a');
a.href = arguments[0];
a.download = '';
a.style.display = 'none';
document.body.appendChild(a);
a.click();
a.remove();
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def safe_name(url):
    name = unquote(Path(urlparse(url).path).name)
    return re.sub(r'[<>:"/\\|?*]', "_", name) or "policy.pdf"


def clean_title(text, url):
    t = re.sub(r"\s*\(PDF\)\s*$", "", text or "", flags=re.I).strip()
    return t or safe_name(url)


def is_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


import subprocess


def get_chrome_major_version():
    """Detect the installed Google Chrome major version on Windows."""
    registry_keys = [
        r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\Software\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}",
    ]
    for key in registry_keys:
        try:
            out = subprocess.check_output(
                ["reg", "query", key, "/v", "version"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).decode()
            m = re.search(r"version\s+REG_SZ\s+(\d+)", out, re.I)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def build_driver(download_dir, headless):
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1366,900")

    opts.add_argument("--lang=en-US")

    # Configure automatic downloads
    opts.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    })

    major = get_chrome_major_version()
    kwargs = {"options": opts}
    if major:
        kwargs["version_main"] = major

    # Use undetected_chromedriver instead of standard webdriver
    driver = webdriver.Chrome(**kwargs)
    driver.set_script_timeout(30)
    return driver



def page_ready(driver):
    try:
        title = driver.title or ""
        body_len = len(driver.execute_script("return document.body ? document.body.innerText : ''") or "")
    except WebDriverException:
        return False
    return bool(title) and "denied" not in title.lower() and body_len > 500


def wait_for_page(driver, timeout, manual):
    if manual:
        input("\nA Chrome window is open. Wait until the page is fully loaded "
              "(complete any check if shown), then press Enter here... ")
        return page_ready(driver)
    try:
        WebDriverWait(driver, timeout, poll_frequency=1).until(page_ready)
        return True
    except TimeoutException:
        return False


def wait_for_download(folder, before, timeout=90):
    """Return the name of the newly completed file in `folder`, or None."""
    end = time.time() + timeout
    while time.time() < end:
        now = set(os.listdir(folder))
        partial = [f for f in now if f.endswith((".crdownload", ".tmp"))]
        new_done = [f for f in (now - before) if not f.endswith((".crdownload", ".tmp"))]
        if new_done and not partial:
            path = Path(folder) / new_done[0]
            size1 = path.stat().st_size
            time.sleep(0.8)
            if path.exists() and path.stat().st_size == size1 and size1 > 0:
                return new_done[0]
        time.sleep(0.4)
    return None


# In-browser fetch via active authenticated session
FETCH_BLOB_JS = r"""
const url = arguments[0];
const done = arguments[1];
fetch(url)
  .then(r => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.blob();
  })
  .then(blob => {
    const reader = new FileReader();
    reader.onloadend = () => done({status: 'ok', data: reader.result.split(',')[1]});
    reader.readAsDataURL(blob);
  })
  .catch(err => done({status: 'error', message: err.toString()}));
"""


def download_one(driver, url, out_dir):
    expected = out_dir / safe_name(url)
    if expected.exists() and expected.stat().st_size > 0 and is_pdf(expected):
        return "skipped (exists)", expected.name

    # Try direct in-browser fetch (fast and avoids download prompts/blockers)
    try:
        res = driver.execute_async_script(FETCH_BLOB_JS, url)
        if res and res.get("status") == "ok":
            raw = base64.b64decode(res["data"])
            if raw.startswith(b"%PDF"):
                expected.write_bytes(raw)
                return "downloaded", expected.name
    except Exception:
        pass

    before = set(os.listdir(out_dir))
    driver.execute_script(CLICK_DOWNLOAD_JS, url)
    got = wait_for_download(out_dir, before, timeout=60)

    if not got:  # fallback: navigate straight to the PDF
        before = set(os.listdir(out_dir))
        try:
            driver.get(url)
        except WebDriverException:
            pass
        got = wait_for_download(out_dir, before, timeout=60)
        try:
            driver.back()
        except WebDriverException:
            pass

    if not got:
        return "FAILED: no file received (timeout)", ""

    path = out_dir / got
    if not is_pdf(path):
        path.unlink(missing_ok=True)
        return "FAILED: file was not a PDF (blocked page?)", ""

    if got != expected.name and not expected.exists():
        path.replace(expected)
        got = expected.name
    return "downloaded", got


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://www.aetnabetterhealth.com/illinois-medicaid/providers/clinical-payment-policies.html")
    ap.add_argument("--out", default="aetna_il_payment_policies")
    ap.add_argument("--include-utilization", action="store_true",
                    help="also download the 'Utilization management policies' section")
    ap.add_argument("--all-pdfs", action="store_true",
                    help="download every PDF found on the page (ignores sections)")
    ap.add_argument("--list-only", action="store_true", help="print links, don't download")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser (the site often blocks this)")
    ap.add_argument("--manual", action="store_true",
                    help="pause so you can confirm the page loaded / pass any check")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for the page to render")
    ap.add_argument("--delay", type=float, default=1.0, help="pause between downloads")
    ap.add_argument("--debug-html", action="store_true", help="save rendered DOM to debug_page.html")
    args = ap.parse_args()

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    driver = build_driver(out_dir, args.headless)
    try:
        print("Loading page...")
        driver.get(args.url)

        if not wait_for_page(driver, args.wait, args.manual):
            print(f"\nPage did not render real content (title: {driver.title!r}).\n"
                  "Try:  --manual  (watch the window, complete any check, press Enter)\n"
                  "      --wait 120\n"
                  "Also make sure you are not running headless and that your VPN/proxy "
                  "isn't blocked by the site.", file=sys.stderr)
            sys.exit(1)
        print(f"Loaded: {driver.title!r}")

        driver.execute_script(EXPAND_JS)
        time.sleep(1)

        if args.debug_html:
            Path("debug_page.html").write_text(driver.page_source, encoding="utf-8")

        # ---- collect links ----
        jobs = []  # (category, title, url)
        if args.all_pdfs:
            for item in driver.execute_script(EXTRACT_ALL_PDFS_JS):
                jobs.append(("all", clean_title(item["text"], item["href"]), item["href"]))
        else:
            sections = [("payment", PAYMENT_TITLE, [UM_TITLE])]
            if args.include_utilization:
                sections.append(("utilization", UM_TITLE, [PAYMENT_TITLE]))
            for category, title, others in sections:
                items = driver.execute_script(EXTRACT_SECTION_JS, title, others)
                print(f"Section '{title}': {len(items)} PDF link(s)")
                for item in items:
                    jobs.append((category, clean_title(item["text"], item["href"]), item["href"]))

        if not jobs:
            print("\nNo PDF links found. Run with --debug-html and inspect debug_page.html, "
                  "or try --all-pdfs.", file=sys.stderr)
            sys.exit(1)

        print()
        if args.list_only:
            for i, (cat, title, url) in enumerate(jobs, 1):
                print(f"{i:>3}. [{cat}] {title}\n     {url}")
            return

        print(f"Downloading {len(jobs)} PDF(s) to: {out_dir}\n")
        manifest = []
        ok = skipped = failed = 0
        for i, (cat, title, url) in enumerate(jobs, 1):
            status, fname = download_one(driver, url, out_dir)
            if status.startswith("downloaded"):
                ok += 1
            elif status.startswith("skipped"):
                skipped += 1
            else:
                failed += 1
            print(f"[{i}/{len(jobs)}] {title} -> {status}")
            manifest.append({"category": cat, "title": title, "url": url,
                             "file": fname, "status": status})
            time.sleep(args.delay)

        with open(out_dir / "manifest.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["category", "title", "url", "file", "status"])
            w.writeheader()
            w.writerows(manifest)

        print(f"\nDone. {ok} downloaded, {skipped} skipped, {failed} failed.")
        print(f"Manifest: {out_dir / 'manifest.csv'}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
