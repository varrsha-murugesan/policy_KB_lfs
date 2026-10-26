import os
import re
import csv
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# PATHS  –  must match scraper_download.py
# ============================================================
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
MEDICARE_DIR    = os.path.join(BASE_DIR, "medicare_advantage_plans")
COMMERCIAL_DIR  = os.path.join(BASE_DIR, "commercial_plans")
CSV_PATH        = os.path.join(BASE_DIR, "policy_title_uhc.csv")
JSON_PATH       = os.path.join(BASE_DIR, "extracted_contents_reimbursement")

# ============================================================
# MAKE A SAFE FILENAME  (no path-separator or illegal chars)
# ============================================================
def safe_filename(title):
    # Strip characters Windows disallows in filenames
    safe = re.sub(r'[\\/:*?"<>|]', "", title)
    # Collapse whitespace
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe

# ============================================================
# RENAME
# ============================================================
def rename_files():
    renamed  = 0
    skipped  = 0
    errors   = 0

    if not os.path.isfile(CSV_PATH):
        print(f"  [INFO] CSV mapping file not found: {CSV_PATH}")
        print("  Run scraper.py first to generate the CSV mapping.")
        return

    with open(CSV_PATH, newline="", encoding="utf-16") as f:
        reader = csv.DictReader(f)
        for row in reader:
            index          = row["index"].strip()
            line_of_biz    = row["line_of_business"].strip()
            category       = row["category"].strip()
            original_title = row["policy_title"].strip()

            # ------------------------------------------------
            # RESOLVE DIRECTORY
            # ------------------------------------------------
            if line_of_biz == "medicare_advantage_plans":
                folder = os.path.join(MEDICARE_DIR, category)
            elif line_of_biz == "commercial_plans":
                folder = os.path.join(COMMERCIAL_DIR, category)
                

            # ------------------------------------------------
            # SOURCE FILE  (numeric name written by downloader)
            # ------------------------------------------------
            src = os.path.join(folder, f"{index}.pdf")
            if not os.path.isfile(src):
                print(f"  [SKIP] Not found: {src}")
                skipped += 1
                continue
            folder1 = JSON_PATH
            src1 = os.path.join(folder1, f"{index}.json")
            if not os.path.isfile(src1):
                print(f"  [SKIP] Not found: {src1}")
                skipped += 1
                continue

            # ------------------------------------------------
            # BUILD TARGET FILENAME
            # ------------------------------------------------
            safe_title  = safe_filename(original_title)

            # Trim so the FULL PATH stays under 259 chars
            # (259 + null terminator = 260, Windows MAX_PATH)
            suffix       = ".pdf"
            suffix1       = ".json"
            max_stem_len = 259 - len(folder) - 1 - len(suffix)  # 1 for os.sep
            max_stem_len = max(1, min(max_stem_len, 200))        # cap at 200 too
            safe_title   = safe_title[:max_stem_len].strip()

            dst = os.path.join(folder, f"{safe_title}{suffix}")
            dst1 = os.path.join(folder1, f"{safe_title}_{line_of_biz}{suffix1}")

            # ------------------------------------------------
            # RENAME
            # ------------------------------------------------
            try:
                os.rename(src, dst)
                os.rename(src1, dst1)
                print(f"  [OK]   {index}.pdf  ->  {safe_title}.pdf")
                renamed += 1
            except OSError as exc:
                print(f"  [ERR]  {index}.pdf  ->  {safe_title}.pdf\n"
                      f"         {exc}")
                errors += 1

    print("\n" + "=" * 70)
    print("RENAME COMPLETE")
    print("=" * 70)
    print(f"  Renamed : {renamed}")
    print(f"  Skipped : {skipped}")
    print(f"  Errors  : {errors}")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 70)
    print("RENAMING FILES USING CSV MAPPING")
    print("=" * 70)
    rename_files()
