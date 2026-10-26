import os
import sys
import argparse
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import re
import csv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# CATEGORY URLS
# ============================================================
CATEGORIES = {
    "medicare_advantage_plans": {
        "Reimbursement": (
            "https://www.uhcprovider.com/en/policies-protocols/"
            "medicare-advantage-policies/"
            "medicare-advantage-reimbursement-policies.html"
        ),
        "Medical_and_Drug": (
            "https://www.uhcprovider.com/en/policies-protocols/"
            "medicare-advantage-policies/"
            "medicare-advantage-medical-policies.html"
        ),
    },
    "commercial_plans": {
        "Reimbursement": (
            "https://www.uhcprovider.com/en/policies-protocols/"
            "commercial-policies/"
            "commercial-reimbursement-policies.html"
        ),
        "Medical_and_Drug": (
            "https://www.uhcprovider.com/en/policies-protocols/"
            "commercial-policies/"
            "commercial-medical-drug-policies.html"
        ),
    },
}

# ============================================================
# OUTPUT DIRECTORIES
# ============================================================
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
MEDICARE_DIR  = os.path.join(BASE_DIR, "medicare_advantage_plans")
COMMERCIAL_DIR = os.path.join(BASE_DIR, "commercial_plans")

# ============================================================
# CSV  –  columns: index | line_of_business | category | policy_title
# "index" is the numeric filename (no extension) used on disk.
# ============================================================
CSV_PATH = os.path.join(BASE_DIR, "policy_title_uhc.csv")

# ============================================================
# GLOBAL COUNTER  –  each downloaded file gets the next number
# ============================================================
file_counter = 0

# ============================================================
# DATE PARSER
# ============================================================
def parse_date(date_str):
    clean_date = date_str.strip().replace("Last Published ", "")
    for fmt in ("%m.%d.%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(clean_date, fmt)
        except ValueError:
            continue
    return None

def clean_policy_title(title):

    """
    Clean the UHC policy title for use as the local PDF filename.

    Example:

    Acupuncture Policy Professional - Reimbursement Policy -
    UnitedHealthcare Commercial and Individual Exchange

    becomes:

    Acupuncture Policy
    """

    # --------------------------------------------------------
    # Remove everything beginning with:
    # " - Reimbursement Policy"
    # --------------------------------------------------------

    clean_title = re.split(
        r"\s*Policy\s*", title, maxsplit=1, flags=re.IGNORECASE
    )[0].strip() + " Policy"

    # --------------------------------------------------------
    # Remove billing class from the end
    #
    # Examples:
    # Professional
    # Facility
    # Professional and Facility
    # --------------------------------------------------------

    clean_title = re.sub(
        r",?\s*(Professional and Facility|Professional|Facility)\s*$",
        "",
        clean_title,
        flags=re.IGNORECASE,
    ).strip()

    # --------------------------------------------------------
    # Remove any extra spaces
    # --------------------------------------------------------

    clean_title = re.sub(r"\s+", " ", clean_title).strip()

    return clean_title

# ============================================================
# SCRAPER
# ============================================================
def scrape_and_download(line_of_business, category_name, url,
                        base_output_dir, csv_writer, limit=200):
    global file_counter

    print(f"\n{'=' * 70}")
    print(f"Line of Business : {line_of_business}")
    print(f"Category         : {category_name}")
    print(f"URL              : {url}")
    print(f"{'=' * 70}")

    # --------------------------------------------------------
    # LOAD PAGE
    # --------------------------------------------------------
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Failed to load page: {exc}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    policies = {}

    # --------------------------------------------------------
    # FIND POLICY ENTRIES
    # --------------------------------------------------------
    for item in soup.find_all(["div", "li", "article"]):
        date_tag = item.find("p", class_=re.compile("list-date"))
        link_tag = item.find("a", href=True)
        if not date_tag or not link_tag:
            continue

        pub_date = parse_date(date_tag.get_text(strip=True))
        if not pub_date:
            continue

        href = link_tag.get("href", "").strip()
        if not href or href.startswith("javascript:") or href.startswith("#") or not href.lower().endswith(".pdf"):
            continue

        raw_title = link_tag.get_text(strip=True)
        if not raw_title or raw_title.lower() in ("close", "skip secondary navigation", "skip to main content"):
            continue

        title = clean_policy_title(raw_title)
        if not title:
            continue

        pdf_url = urljoin(url, href)

        # Keep only the latest version of each title
        if title not in policies or pub_date > policies[title]["date"]:
            policies[title] = {"date": pub_date, "url": pdf_url}

    # --------------------------------------------------------
    # CREATE OUTPUT DIRECTORY
    # --------------------------------------------------------
    output_dir = os.path.join(base_output_dir, category_name)
    os.makedirs(output_dir, exist_ok=True)
    print(f"\nLatest policies found: {len(policies)}")

    # --------------------------------------------------------
    # DOWNLOAD  –  save as <number>.pdf, write mapping to CSV
    # --------------------------------------------------------
    for title, data in policies.items():
        if limit is not None and file_counter >= limit:
            break
        try:
            print(f"\nDownloading:")
            print(f"  Title   : {title}")
            print(f"  Date    : {data['date'].date()}")
            print(f"  URL     : {data['url']}")

            pdf_res = requests.get(data["url"], timeout=60)
            pdf_res.raise_for_status()

            # Increment counter BEFORE saving so index starts at 1
            file_counter += 1
            file_path = os.path.join(output_dir, f"{file_counter}.pdf")

            with open(file_path, "wb") as f:
                f.write(pdf_res.content)

            # Write mapping:  index, line_of_business, category, original title
            csv_writer.writerow([file_counter, line_of_business,
                                  category_name, title])

            print(f"  Saved as: {file_counter}.pdf  ->  {file_path}")

        except requests.RequestException as exc:
            print(f"  FAILED to download: {exc}")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape UHC policy PDFs and record mapping.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum policies to download (default: 200)")
    args = parser.parse_args()

    with open(CSV_PATH, "w", newline="", encoding="utf-16") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["index", "line_of_business", "category", "policy_title"])
        

        for line_of_business, categories in CATEGORIES.items():
            base_output_dir = (
                MEDICARE_DIR
                if line_of_business == "medicare_advantage_plans"
                else COMMERCIAL_DIR
            )
            if args.limit is not None and file_counter >= args.limit:
                break
            for category_name, target_url in categories.items():
                if args.limit is not None and file_counter >= args.limit:
                    break
                scrape_and_download(
                    line_of_business, category_name, target_url,
                    base_output_dir, writer, limit=args.limit
                )

    print("\n" + "=" * 70)
    print("DOWNLOAD COMPLETE")
    print("=" * 70)
    print(f"\nMedicare Advantage : {MEDICARE_DIR}")
    print(f"Commercial         : {COMMERCIAL_DIR}")
    print(f"CSV (mapping file) : {CSV_PATH}")
    print(f"Total files saved  : {file_counter}")
