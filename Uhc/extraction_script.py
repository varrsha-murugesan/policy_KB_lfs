TOP_LEVEL_SECTION_NAMES = {
    "application",
    "applications",
    "policy",
    "overview",
    "reimbursement guidelines",
    "reimbursement guideline",
    "coverage criteria",
    "definitions",
    "definition",
    "questions and answers",
    "question and answers",
    "codes",
    "applicable codes",
    "procedure codes",
    "procedure code(s)",
    "attachments",
    "resources",
    "history",
    "history section",
}

NON_SUBSECTION_PARENT_TITLES = {
    "questions and answers",
    "question and answers",
    "definitions",
    "definition",
    "resources",
    "history",
    "history section",
    "codes",
    "applicable codes",
    "procedure codes",
    "procedure code(s)",
    "attachments",
}

import os
import re
import sys
import json
import csv
import unicodedata
from collections import Counter
import pymupdf
import requests
from tqdm import tqdm

# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Input structure:
#   medicare_advantage_plans/
#       Medical_and_Drug/
#       Reimbursement/
#   commercial_plans/
#       Medical_and_Drug/
#       Reimbursement/
#
# All extracted JSON files are written into ONE output folder.
MEDICARE_ADVANTAGE_DIR = os.path.join(
    SCRIPT_DIR, "medicare_advantage_plans/Reimbursement"
)
COMMERCIAL_DIR = os.path.join(SCRIPT_DIR, "commercial_plans/Reimbursement")

INPUT_DIRS = [
    MEDICARE_ADVANTAGE_DIR,
    COMMERCIAL_DIR,
]

TOTAL_NO_OF_FILES = 242

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "extracted_contents_reimbursement")
OUTPUT_RULES_FILE = os.path.join(SCRIPT_DIR, "extraction_rules.json")
OUTPUT_TITLES_CSV = os.path.join(SCRIPT_DIR, "policy_titles.csv")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "policy_title_uhc.csv")
DATE_PREFIX_PATTERN = re.compile(r"^\s*\d{1,4}[-_./]\d{1,2}[-_./]\d{2,4}")

CPT_CODE_REGEX = r"\b[A-Z0-9]{1}[0-9]{3}[A-Z0-9]{1}\b"

ICD_CODE_REGEX = r"\b[A-Za-z][0-9][A-Za-z0-9](?:\.[A-Za-z0-9]{1,4})?\b"

# Modifier regex: two-character alphanumeric codes (e.g. 25, 59, TC, QW, LT, RT)
# Requires the word "modifier" nearby (within ±10 words) to avoid false positives.
MODIFIER_EXTRACT_REGEX = re.compile(r"\b([A-Z]{2}|\d{2}|[A-Z]\d|\d[A-Z])\b")

QUESTION_ANSWER_BLOCK_REGEX = re.compile(
    r"Q:[\s\S]*?A:[\s\S]*?" r"(?=(?:\s+\d+\s+)?Q:|(?:\s*$))",
    re.MULTILINE,
)

MODIFIER_WORD_REGEX = re.compile(r"\bmodifiers?\b", re.IGNORECASE)

MODIFIER_CODE_REGEX = re.compile(r"\b(?:\d{2}|[A-Z]{2}|[A-Z]\d)\b", re.IGNORECASE)

TOC_TITLE_VARIANTS = ("table of contents", "contents")
TOC_MAX_SCAN_LINES = 250
TOC_LEVEL_X_TOLERANCE = 5.0


# ============================================================
# TEXT NORMALIZATION
# ============================================================


def normalize_heading_text(text):
    return text.strip().strip(":.").strip().lower()


def normalize_heading_loose(text):
    words = normalize_heading_text(text).split()
    return " ".join(
        word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words
    )


def heading_text_matches(text, target_heading):
    return normalize_heading_text(text) == normalize_heading_text(
        target_heading
    ) or normalize_heading_loose(text) == normalize_heading_loose(target_heading)


# PDFs frequently embed "smart" typography (curly quotes, en/em
# dashes, bullet glyphs, ellipsis characters, non-breaking spaces)
# that renders fine visually but breaks downstream regex matching,
# JSON diffing, and plain-ASCII consumers. Map the common offenders
# to their plain ASCII/UTF-8 equivalents.
UNICODE_PUNCTUATION_MAP = {
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote / apostrophe
    "\u201a": "'",  # single low-9 quote
    "\u201b": "'",  # single high-reversed-9 quote
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u201e": '"',  # double low-9 quote
    "\u201f": '"',  # double high-reversed-9 quote
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2022": "-",  # bullet
    "\u2023": "-",  # triangular bullet
    "\u2043": "-",  # hyphen bullet
    "\u25aa": "-",  # black small square (used as a bullet)
    "\u25cf": "-",  # black circle (used as a bullet)
    "\u00b7": "-",  # middle dot (used as a bullet)
    "\u2026": "...",  # horizontal ellipsis
    "\u00a0": " ",  # non-breaking space
    "\u202f": " ",  # narrow no-break space
    "\u200b": "",  # zero-width space
    "\ufeff": "",  # byte order mark / zero-width no-break space
    "\u00ae": "",  # registered trademark (R)
    "\u2122": "",  # trademark (TM)
    "\u00a9": "(c)",  # copyright (c)
    "\u2756": "-",  # black diamond minus white X bullet
    "\ufffd": "",  # replacement character
}


def normalize_unicode_punctuation(text):
    """
    Normalize "smart" typography introduced by PDF fonts down to
    plain ASCII/UTF-8 equivalents.

    NFKD decomposes compatibility characters (e.g. ligatures, some
    stylized punctuation variants) into their base + combining-mark
    form; running the explicit punctuation map afterward catches the
    curly quotes, dashes, and bullet glyphs that NFKD alone does not
    rewrite (they are not compatibility-decomposable, just distinct
    code points).
    """
    if not text:
        return text

    text = unicodedata.normalize("NFKD", text)
    text = "".join(UNICODE_PUNCTUATION_MAP.get(char, char) for char in text)

    return text


def clean_cell_text(text):
    if text is None:
        return ""

    text = str(text)
    text = normalize_unicode_punctuation(text)
    text = text.replace("\u0000", "")

    # If a PDF wraps a filename after a hyphen, PyMuPDF can return
    # "Age-Based-Proc-Codes- Policy-List.pdf". Remove that artificial
    # space while preserving normal word spacing elsewhere.
    text = re.sub(r"(?<=[A-Za-z0-9])- +(?=[A-Za-z0-9])", "-", text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", " ", text)

    return text.strip()


def join_lines(lines):
    return " ".join(
        clean_cell_text(line["text"]) for line in lines if clean_cell_text(line["text"])
    )


# ============================================================
# PDF TEXT + LAYOUT EXTRACTION
# ============================================================


def is_uhc_blue(fill):
    if fill is None or len(fill) < 3:
        return False

    r, g, b = fill[:3]

    if r > 1.0 or g > 1.0 or b > 1.0:
        r, g, b = r / 255.0, g / 255.0, b / 255.0

    if r > 0.92 and g > 0.92 and b > 0.92:
        return False

    intensity = 0.299 * r + 0.587 * g + 0.114 * b
    return intensity < 0.88


def get_blue_regions(page):
    regions = []

    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        fill = drawing.get("fill")

        if rect is None:
            continue

        if not is_uhc_blue(fill):
            continue

        if rect.width < 250:
            continue

        if rect.height > 50:
            continue

        regions.append(pymupdf.Rect(rect))

    return regions


def merge_blue_regions(regions):
    if not regions:
        return []

    regions = sorted(regions, key=lambda r: (r.y0, r.x0))
    merged = []

    for region in regions:
        if not merged:
            merged.append(region)
            continue

        previous = merged[-1]

        horizontal_overlap = region.x0 <= previous.x1 and region.x1 >= previous.x0

        vertical_close = region.y0 <= previous.y1 + 5 and region.y1 >= previous.y0 - 5

        if horizontal_overlap and vertical_close:
            merged[-1] = pymupdf.Rect(
                min(previous.x0, region.x0),
                min(previous.y0, region.y0),
                max(previous.x1, region.x1),
                max(previous.y1, region.y1),
            )
        else:
            merged.append(region)

    return merged


def is_inside_blue(line_bbox, blue_regions):
    line_rect = pymupdf.Rect(line_bbox)

    for region in blue_regions:
        center = pymupdf.Point(
            (line_rect.x0 + line_rect.x1) / 2,
            (line_rect.y0 + line_rect.y1) / 2,
        )

        if region.contains(center):
            return True

        intersection = line_rect & region

        if intersection.is_empty:
            continue

        line_area = line_rect.width * line_rect.height

        if line_area <= 0:
            continue

        intersection_area = intersection.width * intersection.height

        if intersection_area / line_area >= 0.30:
            return True

    return False


def extract_lines(doc):
    lines = []

    for page_number, page in enumerate(doc, start=1):
        blue_regions = merge_blue_regions(get_blue_regions(page))

        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if "lines" not in block:
                continue

            for line in block["lines"]:
                spans = line["spans"]

                if not spans:
                    continue

                text = "".join(span["text"] for span in spans).strip()

                if not text:
                    continue

                font_size = round(max(span["size"] for span in spans), 1)

                bold = any(
                    (
                        "Bold" in span["font"]
                        or "Black" in span["font"]
                        or (span["flags"] & 16)
                    )
                    for span in spans
                    if span["text"].strip()
                )

                lines.append(
                    {
                        "page": page_number,
                        "text": text,
                        "font": font_size,
                        "bold": bold,
                        "x": round(line["bbox"][0], 1),
                        "y": round(line["bbox"][1], 1),
                        "bbox": line["bbox"],
                        "blue": is_inside_blue(line["bbox"], blue_regions),
                    }
                )

    for index, line in enumerate(lines):
        line["index"] = index

    return lines


# ============================================================
# HEADER / FOOTER CLEANUP
# ============================================================

PROCEDURE_CODE_LABELS = {
    "procedure code(s)",
    "procedure codes",
    "procedure code",
}

CONDITION_LABELS = {
    "condition(s)",
    "conditions",
    "condition",
}

MODIFIER_LABELS = {
    "laterality and/or anatomical modifiers",
    "laterality and/or anatomical modifier",
    "laterality and/or modifiers",
    "modifiers",
    "modifier",
}


def clean_lines(lines, page_count):
    # Only track frequency of lines appearing at header/footer margins (top/bottom of page)
    header_footer_text_pages = Counter()
    for line in lines:
        if line.get("y", 0) < 110 or line.get("y", 0) > 730:
            header_footer_text_pages[line["text"]] += 1

    cleaned = []

    for line in lines:
        text = line["text"].strip()
        normalized = normalize_heading_text(text)

        protected = (
            normalized in PROCEDURE_CODE_LABELS
            or normalized in CONDITION_LABELS
            or normalized in MODIFIER_LABELS
            or normalized in TOP_LEVEL_SECTION_NAMES
        )

        if protected:
            cleaned.append(line)
            continue

        # Strip known running headers/footers
        if re.search(r"Proprietary\s+information\s+of\s+UnitedHealthcare", text, re.I):
            continue

        if re.match(r"^UnitedHealthcare.*Reimbursement\s+Policy", text, re.I):
            continue

        if re.match(r"^Policy\s+Number\s+[A-Z0-9]+", text, re.I):
            continue

        if text in {"CMS 1500", "UB04", "CMS 1450", "CMS-1500", "UB-04", "CMS-1450"}:
            continue

        if re.match(r"^Page\s+\d+\s+of\s+\d+$", text, re.I) or re.match(
            r"^\d+\s+of\s+\d+$", text
        ):
            continue

        if "Copyright" in text and (
            "United HealthCare" in text or "UnitedHealthcare" in text
        ):
            continue

        # If repeated across almost all pages at header/footer margins
        if page_count >= 3 and header_footer_text_pages[line["text"]] >= page_count - 1:
            if line.get("y", 0) < 110 or line.get("y", 0) > 730:
                continue

        cleaned.append(line)

    for index, line in enumerate(cleaned):
        line["index"] = index

    return cleaned


# ============================================================
# HEADING EXTRACTION
# ============================================================


def find_target_occurrences(
    lines,
    target_heading,
    search_start=0,
    search_end=None,
):
    occurrences = []

    end = search_end if search_end is not None else len(lines)

    for index in range(search_start, end):
        line = lines[index]

        if heading_text_matches(line["text"], target_heading):
            occurrence = line.copy()
            occurrence["index"] = index
            occurrences.append(occurrence)

    return occurrences


def _find_heading_by_bbox_fallback(occurrences, verbose=False):
    """
    Fallback heading detector used when no candidate line was drawn in
    UHC blue (e.g. a policy PDF that renders section headers in plain
    bold black text, or a scanned/re-flattened document where the fill
    color metadata is lost).

    Headings in these documents are still reliably identifiable by
    layout even without color: they sit at a consistent left-edge
    x-coordinate and are set in a distinct (bold and/or larger) font
    compared to body text. Group candidates by (x-position, font size)
    and treat the most common combination as the heading style, mirroring
    what the blue-color path does with font size alone.
    """
    if not occurrences:
        return None

    candidates = [occurrence for occurrence in occurrences if occurrence.get("bold")]

    if not candidates:
        candidates = occurrences

    def bbox_key(occurrence):
        # Round the left x-coordinate to a coarse bucket so that
        # sub-pixel PDF rendering differences between otherwise
        # identically-indented headings don't split them into
        # different groups.
        x = occurrence.get("x", 0.0) or 0.0
        bucket = round(x / TOC_LEVEL_X_TOLERANCE) * TOC_LEVEL_X_TOLERANCE
        return (bucket, occurrence.get("font"))

    position_counts = Counter(bbox_key(occurrence) for occurrence in candidates)

    expected_key = position_counts.most_common(1)[0][0]

    same_position = [
        occurrence for occurrence in candidates if bbox_key(occurrence) == expected_key
    ]

    return same_position[0] if same_position else candidates[0]


def find_valid_heading(
    occurrences,
    heading_text=None,
    verbose=False,
):
    blue_occurrences = [occurrence for occurrence in occurrences if occurrence["blue"]]

    if blue_occurrences:
        font_counts = Counter(occurrence["font"] for occurrence in blue_occurrences)

        expected_font = font_counts.most_common(1)[0][0]

        same_font = [
            occurrence
            for occurrence in blue_occurrences
            if occurrence["font"] == expected_font
        ]

        return same_font[0] if same_font else blue_occurrences[0]

    # No exact blue-font match found. Rather than giving up on the
    # heading entirely, fall back to coordinate/typography-based
    # detection so sections like "Reimbursement Guidelines" are still
    # located in documents that don't use the expected blue color.
    return _find_heading_by_bbox_fallback(
        occurrences,
        verbose=verbose,
    )


def resolve_heading_occurrence(
    lines,
    title,
    search_start=0,
    verbose=False,
):
    occurrences = find_target_occurrences(
        lines,
        title,
        search_start=search_start,
    )

    return find_valid_heading(
        occurrences,
        heading_text=title,
        verbose=verbose,
    )


def find_next_blue_heading(
    lines,
    start_index,
    heading_font_size,
):
    for index in range(start_index + 1, len(lines)):
        line = lines[index]

        if line["blue"] and line["font"] == heading_font_size:
            return index

    return None


def extract_heading_content(lines, heading):
    start_index = heading["index"]
    heading_font_size = heading["font"]

    next_heading_index = find_next_blue_heading(
        lines,
        start_index,
        heading_font_size,
    )

    if next_heading_index is None:
        return (
            lines[start_index + 1 :],
            None,
        )

    return (
        lines[start_index + 1 : next_heading_index],
        next_heading_index,
    )


# ============================================================
# TOC SUPPORT
# ============================================================


def find_toc_start_index(lines):
    for index, line in enumerate(lines):
        if line["text"].strip().lower() in TOC_TITLE_VARIANTS:
            return index

    return None


_TOC_LEADER_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*[.\u2024\u2026\u00b7\u2022\- ]{2,}\s*(?P<page>\d{1,4})\s*$"
)

_TOC_SIMPLE_PATTERN = re.compile(
    r"^(?P<title>[A-Za-z][A-Za-z0-9,/&()'\- ]{1,90}?)\s+(?P<page>\d{1,4})\s*$"
)


def parse_toc_entry(text):
    text = text.strip()

    if not text:
        return None

    match = _TOC_LEADER_PATTERN.match(text)

    if not match:
        match = _TOC_SIMPLE_PATTERN.match(text)

    if match:
        title = match.group("title").strip(" .\u2024\u2026\u00b7\u2022-")

        try:
            page_number = int(match.group("page"))
        except ValueError:
            return None

        return title, page_number

    if len(text) <= 80 and not text.endswith(".") and not text.endswith(","):
        return text, None

    return None


def extract_toc_entries(
    lines,
    toc_start_index,
    max_scan=TOC_MAX_SCAN_LINES,
):
    entries = []
    toc_start_page = lines[toc_start_index]["page"]

    scan_end = min(len(lines), toc_start_index + 1 + max_scan)

    for index in range(toc_start_index + 1, scan_end):
        line = lines[index]

        # Stop if we encounter a blue section header on a subsequent page (start of document body)
        if line["blue"] and line["page"] != toc_start_page:
            break

        parsed = parse_toc_entry(line["text"])

        if parsed is None:
            # If on another page and line is not a valid TOC entry, stop scanning
            if line["page"] != toc_start_page:
                break
            continue

        title, page_number = parsed
        norm_t = normalize_heading_text(title)

        if "unitedhealthcare" in norm_t or "reimbursement policy" in norm_t:
            if line["page"] != toc_start_page:
                break
            continue

        entries.append(
            {
                "title": normalize_unicode_punctuation(title).strip(),
                "page": page_number,
                "x": line["x"],
                "y": line["y"],
                "toc_line_index": index,
                "children": [],
            }
        )

    return entries


def assign_toc_levels(
    entries,
    x_tolerance=TOC_LEVEL_X_TOLERANCE,
):
    if not entries:
        return

    xs = sorted(set(round(e["x"], 1) for e in entries))

    clusters = []

    for x in xs:
        if clusters and abs(x - clusters[-1][-1]) <= x_tolerance:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    level_by_x = {}

    for level, cluster in enumerate(clusters, start=1):
        for x in cluster:
            level_by_x[x] = level

    for entry in entries:
        entry["level"] = level_by_x[round(entry["x"], 1)]


def build_toc_tree(entries):
    assign_toc_levels(entries)

    roots = []
    stack = []

    for entry in entries:
        level = entry["level"]

        while stack and stack[-1][0] >= level:
            stack.pop()

        if stack:
            stack[-1][1]["children"].append(entry)
        else:
            roots.append(entry)

        stack.append((level, entry))

    return roots


def flatten_toc_tree(tree):
    flat = []

    for node in tree:
        flat.append(node)
        flat.extend(flatten_toc_tree(node.get("children", [])))

    return flat


# ============================================================
# GENERIC NO-TOC TREE
# ============================================================


def is_subsection_heading(line):
    if not line["bold"] or line["blue"]:
        return False

    text = line["text"].strip()

    if not text:
        return False

    normalized = normalize_heading_text(text)

    # Exclude single digits, numbers, dates, CPT codes, Q&A prefixes
    if text.isdigit() or re.match(r"^\d+[\.:\s]*$", text):
        return False
    if re.match(r"^\d{1,4}[-/\.]\d{1,2}[-/\.]\d{2,4}", text):
        return False
    if re.match(r"^[0-9A-Z]{5}$", text):
        return False
    if text.lower().startswith("q:") or text.lower().startswith("a:"):
        return False

    if (
        normalized in PROCEDURE_CODE_LABELS
        or normalized in CONDITION_LABELS
        or normalized in MODIFIER_LABELS
        or normalized in TOP_LEVEL_SECTION_NAMES
    ):
        return False

    if len(text) > 100:
        return False

    text_clean = text.rstrip(".").strip()
    if (
        text.endswith(".")
        and len(text_clean.split()) > 10
        and not any(k in normalized for k in ["reimbursement", "guideline", "overview"])
    ):
        return False

    return True


def line_in_any_table(line, tables):
    if not tables:
        return False

    if "bbox" not in line:
        return False

    line_rect = pymupdf.Rect(line["bbox"])

    for table in tables:
        if table["page"] != line["page"]:
            continue

        table_rect = pymupdf.Rect(table["bbox"])

        if line_rect.intersects(table_rect):
            return True

    return False


def build_tree_without_toc(
    lines,
    tables=None,
):
    top_heading_lines = []

    for line in lines:
        text = line.get("text", "").strip()
        norm = normalize_heading_text(text)

        # Exclude dates, CPT codes, URLs, table labels, table cells, dangling text
        if not text or len(text) > 80:
            continue
        if re.match(r"^\d{1,4}[-/\.]\d{1,2}[-/\.]\d{2,4}", norm):
            continue
        if re.match(r"^[0-9A-Z]{5}$", norm):
            continue
        if (
            norm.startswith("http://")
            or norm.startswith("https://")
            or norm.startswith("www.")
        ):
            continue
        if (
            norm in PROCEDURE_CODE_LABELS
            or norm in CONDITION_LABELS
            or norm in MODIFIER_LABELS
        ):
            continue
        if line_in_any_table(line, tables) and norm not in TOP_LEVEL_SECTION_NAMES:
            continue
        if text.endswith(",") or text.endswith(":"):
            continue

        if (
            line.get("blue")
            and line.get("font", 0) < 13.0
            and (
                norm in TOP_LEVEL_SECTION_NAMES
                or line.get("bold")
                or line.get("font", 0) >= 10.0
            )
        ):
            top_heading_lines.append(line)
        elif (
            (line.get("bold") or line.get("blue"))
            and norm in TOP_LEVEL_SECTION_NAMES
            and line.get("font", 0) >= 9.5
        ):
            if not any(h["index"] == line["index"] for h in top_heading_lines):
                top_heading_lines.append(line)

    top_heading_lines.sort(key=lambda x: x["index"])

    tree = []

    for idx, heading in enumerate(top_heading_lines):
        start = heading["index"]

        end = (
            top_heading_lines[idx + 1]["index"]
            if idx + 1 < len(top_heading_lines)
            else len(lines)
        )

        content = lines[start + 1 : end]

        node = {
            "title": heading["text"].strip(),
            "level": 1,
            "confirmed": True,
            "page": heading["page"],
            "matched_via": "blue" if heading.get("blue") else "bold",
            "match_type": "blue" if heading.get("blue") else "bold",
            "heading": heading,
            "content": content,
            "own_content": content,
            "next_index": end,
            "children": [],
        }

        # Bold text inside an actual table is DATA, not a semantic
        # child heading. This prevents "Definitive Source", Q&A rows,
        # attachment filenames, etc. from becoming fake subsections.
        child_candidates = []
        norm_title = normalize_heading_text(node["title"])

        if norm_title not in NON_SUBSECTION_PARENT_TITLES:
            for line in content:
                if is_subsection_heading(line) and not line_in_any_table(line, tables):
                    child_candidates.append(line)

        for child_pos, child_heading in enumerate(child_candidates):
            child_start = child_heading["index"]

            child_end = (
                child_candidates[child_pos + 1]["index"]
                if child_pos + 1 < len(child_candidates)
                else end
            )

            child_content = lines[child_start + 1 : child_end]

            child = {
                "title": child_heading["text"].strip(),
                "level": 2,
                "confirmed": True,
                "page": child_heading["page"],
                "matched_via": "bold",
                "match_type": "bold",
                "heading": child_heading,
                "content": child_content,
                "own_content": child_content,
                "next_index": child_end,
                "children": [],
            }

            node["children"].append(child)

        if node["children"]:
            first_child_index = min(
                child["heading"]["index"] for child in node["children"]
            )

            node["own_content"] = lines[start + 1 : first_child_index]

        tree.append(node)

    return (tree, flatten_toc_tree(tree))


# ============================================================
# GENERIC TABLE EXTRACTION
# ============================================================


def _tablefinder_extract(page):
    """
    Primary table detector.

    Uses PyMuPDF's native find_tables() so extraction is based on
    actual PDF table geometry rather than section names.
    """
    if not hasattr(page, "find_tables"):
        return []

    try:
        finder = page.find_tables()
        tables = getattr(finder, "tables", []) or []
    except Exception:
        return []

    result = []

    for table in tables:
        try:
            matrix = table.extract()
        except Exception:
            matrix = []

        if not matrix:
            continue

        result.append(
            {
                "bbox": tuple(table.bbox),
                "rows": matrix,
                "page": page.number + 1,
                "source": "pymupdf_find_tables",
            }
        )

    return result


def _vector_line_fallback(page):
    """
    Fallback for older PyMuPDF versions without find_tables().
    Detects vector border lines and reconstructs cells.
    """
    horizontal = []
    vertical = []

    for drawing in page.get_drawings():
        rect = drawing.get("rect")

        if not rect:
            continue

        if rect.width >= 50 and rect.height <= 3:
            horizontal.append((rect.y0, rect.x0, rect.x1))

        if rect.height >= 15 and rect.width <= 3:
            vertical.append((rect.x0, rect.y0, rect.y1))

    if not horizontal or not vertical:
        return []

    def cluster_horizontal(items, tol=1.5):
        groups = []

        for y, x0, x1 in sorted(items):
            target = None

            for group in groups:
                if abs(group["y"] - y) <= tol:
                    target = group
                    break

            if target is None:
                target = {
                    "y": y,
                    "segments": [],
                }
                groups.append(target)

            target["segments"].append((x0, x1))

        for group in groups:
            group["x0"] = min(x0 for x0, _ in group["segments"])
            group["x1"] = max(x1 for _, x1 in group["segments"])

        return groups

    def cluster_vertical(items, tol=1.5):
        groups = []

        for x, y0, y1 in sorted(items):
            target = None

            for group in groups:
                if abs(group["x"] - x) <= tol:
                    target = group
                    break

            if target is None:
                target = {
                    "x": x,
                    "segments": [],
                }
                groups.append(target)

            target["segments"].append((y0, y1))

        for group in groups:
            group["y0"] = min(y0 for y0, _ in group["segments"])
            group["y1"] = max(y1 for _, y1 in group["segments"])

        return groups

    hs = cluster_horizontal(horizontal)

    vs = cluster_vertical(vertical)

    candidates = []

    for v in vs:
        if v["x"] <= page.rect.x0 + 5 or v["x"] >= page.rect.x1 - 5:
            continue

        relevant_h = [
            h
            for h in hs
            if (
                h["x0"] <= v["x"] + 8
                and h["x1"] >= v["x"] - 8
                and h["y"] >= v["y0"] - 3
                and h["y"] <= v["y1"] + 3
            )
        ]

        if len(relevant_h) < 2:
            continue

        ys = sorted(set(round(h["y"], 1) for h in relevant_h))

        for a in range(len(ys) - 1):
            for b in range(a + 1, len(ys)):
                y0 = ys[a]
                y1 = ys[b]

                if y1 - y0 < 12:
                    continue

                inner_vs = [
                    x["x"]
                    for x in vs
                    if (
                        x["x"] > page.rect.x0 + 5
                        and x["x"] < page.rect.x1 - 5
                        and x["y0"] <= y0 + 5
                        and x["y1"] >= y1 - 5
                    )
                ]

                if not inner_vs:
                    continue

                left_candidates = [
                    x["x"]
                    for x in vs
                    if (x["x"] <= v["x"] and x["y0"] <= y0 + 5 and x["y1"] >= y1 - 5)
                ]

                right_candidates = [
                    x["x"]
                    for x in vs
                    if (x["x"] >= v["x"] and x["y0"] <= y0 + 5 and x["y1"] >= y1 - 5)
                ]

                if not left_candidates or not right_candidates:
                    continue

                left = max(left_candidates)

                right = min(right_candidates)

                if right - left < 80:
                    continue

                candidates.append((left, y0, right, y1))

    unique = []

    for bbox in candidates:
        x0, y0, x1, y1 = bbox
        duplicate = False

        for old in unique:
            ox0, oy0, ox1, oy1 = old

            overlap_x = max(0, min(x1, ox1) - max(x0, ox0))

            overlap_y = max(0, min(y1, oy1) - max(y0, oy0))

            area = max(1, (x1 - x0) * (y1 - y0))

            if overlap_x * overlap_y / area > 0.75:
                duplicate = True
                break

        if not duplicate:
            unique.append(bbox)

    tables = []

    for bbox in unique:
        x0, y0, x1, y1 = bbox

        xs = sorted(
            set(
                round(v["x"], 1)
                for v in vs
                if (
                    v["x"] >= x0 - 2
                    and v["x"] <= x1 + 2
                    and v["y0"] <= y0 + 5
                    and v["y1"] >= y1 - 5
                )
            )
        )

        ys = sorted(
            set(
                round(h["y"], 1)
                for h in hs
                if (
                    h["y"] >= y0 - 2
                    and h["y"] <= y1 + 2
                    and h["x0"] <= x1 + 8
                    and h["x1"] >= x0 - 8
                )
            )
        )

        if len(xs) < 3 or len(ys) < 2:
            continue

        rows = []

        for r in range(len(ys) - 1):
            row = []

            for c in range(len(xs) - 1):
                cell_rect = pymupdf.Rect(
                    xs[c] + 1,
                    ys[r] + 1,
                    xs[c + 1] - 1,
                    ys[r + 1] - 1,
                )

                text = clean_cell_text(page.get_text("text", clip=cell_rect))

                row.append(text)

            rows.append(row)

        if rows:
            tables.append(
                {
                    "bbox": bbox,
                    "rows": rows,
                    "page": page.number + 1,
                    "source": "vector_fallback",
                }
            )

    return tables


def _row_dangling_qa_state(row):
    """
    Inspect a single table row for an inline "Q:"/"A:" marker that is
    incomplete, the way a Q&A row looks when a PDF table has been cut
    in half by a page break.

    Returns:
      "dangling_q" - row has a "Q:" marker with no matching "A:"
                     marker (the question got cut off at the bottom
                     of a page).
      "bare_a"     - row has an "A:" marker with no "Q:" marker (the
                     answer that continues at the top of the next
                     page).
      None         - row doesn't look like an inline Q&A fragment.
    """
    cells = [clean_cell_text(cell) for cell in row if clean_cell_text(cell)]

    if not cells:
        return None

    # A leading bare row-number cell (e.g. "1", "2") shouldn't stop us
    # from inspecting the rest of the row for markers.
    text_cells = [cell for cell in cells if not re.fullmatch(r"\d+", cell)]

    if not text_cells:
        return None

    combined = " ".join(text_cells)

    has_q = re.search(r"\bQ:\s*", combined, re.IGNORECASE)
    has_a = re.search(r"\bA:\s*", combined, re.IGNORECASE)

    if has_q and not has_a:
        return "dangling_q"

    if has_a and not has_q:
        return "bare_a"

    return None


def _merge_page_split_qa_tables(all_tables):
    """
    Stitch together tables that PyMuPDF detected as two separate
    tables purely because a Q&A entry was split across a page break
    (e.g. a numbered row like ["1", "Q: ..."] ends page NN with no
    "A:", and the answer text is the first row on page NN+1).

    Without this, each half is parsed independently: the dangling
    question gets misread as a complete pair on the first page, and
    the orphaned answer on the following page has nothing to attach
    to.
    """
    if not all_tables:
        return all_tables

    merged = [all_tables[0]]

    for table in all_tables[1:]:
        prev = merged[-1]

        prev_rows = prev.get("rows") or []
        last_row = next(
            (r for r in reversed(prev_rows) if any(clean_cell_text(c) for c in r)),
            None,
        )

        is_continuation = (
            table.get("page") == prev.get("page", 0) + 1
            and last_row is not None
            and _row_dangling_qa_state(last_row) == "dangling_q"
        )

        if is_continuation:
            prev["rows"] = prev_rows + (table.get("rows") or [])
            continue

        merged.append(table)

    return merged


def detect_tables(doc):
    all_tables = []

    for page in doc:
        tables = _tablefinder_extract(page)

        if not tables:
            tables = _vector_line_fallback(page)

        all_tables.extend(tables)

    return _merge_page_split_qa_tables(all_tables)


def table_title_from_rows(rows):
    if not rows:
        return None

    first = rows[0]

    if not first:
        return None

    first_cell = clean_cell_text(first[0])

    if not first_cell:
        return None

    rest_empty = all(not clean_cell_text(cell) for cell in first[1:])

    if rest_empty:
        return first_cell

    return None


def normalize_table_rows(rows):
    normalized = []

    for row in rows:
        row = [clean_cell_text(cell) for cell in row]

        if any(row):
            normalized.append(row)

    return normalized


def split_q_and_a(text):
    text = clean_cell_text(text)

    q_match = re.search(r"\bQ:\s*", text, re.IGNORECASE)

    a_match = re.search(r"\bA:\s*", text, re.IGNORECASE)

    if not q_match or not a_match or a_match.start() <= q_match.end():
        return None

    return {
        "Q": text[q_match.end() : a_match.start()].strip(),
        "A": text[a_match.end() :].strip(),
    }


QA_SECTION_TITLES = (
    "questions and answers",
    "questions & answers",
    "question and answer",
    "q&a",
    "q & a",
)


def _is_qa_section_title(title):
    if not title:
        return False

    normalized = normalize_heading_text(title)

    return normalized in QA_SECTION_TITLES or (
        "question" in normalized and "answer" in normalized
    )


def parse_qa_grid_rows(rows):
    """
    Parse a PyMuPDF table grid found in a "Questions and Answers"
    section directly, cell row by cell row, instead of flattening the
    whole table into one blob of text and re-splitting it with a
    regex afterward.

    Handles the table layouts that actually show up in these policy
    PDFs:
      - 2 populated columns with no inline "Q:"/"A:" markers:
            [question text, answer text]
        the row IS the Q&A pair by column position.
      - 3+ columns where the first is a row number:
            ["1", question text, answer text]
        the number becomes the pair's key.
      - 1 populated column with inline markers:
            ["Q: ... A: ..."]
        split on the markers.
      - A dangling "Q:" cell whose "A:" arrives in a later row/page
        (common when a question wraps across a page break) is held
        and merged with the row that completes it.
    """
    rows = normalize_table_rows(rows)

    if not rows:
        return []

    detected_title = table_title_from_rows(rows)

    if detected_title and _is_qa_section_title(detected_title):
        rows = rows[1:]

    # A 2-column QA grid often carries its own literal column header
    # row (e.g. ["Question", "Answer"] or ["Q", "A"]) rather than a
    # single merged title cell. Strip that too so it isn't emitted as
    # a bogus first pair.
    if rows:
        header_cells = [normalize_heading_text(cell) for cell in rows[0] if cell]

        if header_cells and all(
            cell in ("question", "questions", "q", "answer", "answers", "a")
            for cell in header_cells
        ):
            rows = rows[1:]

    pairs = []
    pending_q_text = None
    auto_key = 0

    def emit(q_text, a_text, key=None):
        nonlocal auto_key

        if key is None:
            auto_key += 1
            key = str(auto_key)

        pairs.append(
            {
                "key": key,
                "value": {
                    "Q": (q_text or "").strip(),
                    "A": (a_text or "").strip(),
                },
            }
        )

    for row in rows:
        if not any(row):
            continue

        non_empty = [cell for cell in row if cell]

        # -- Single populated cell: look for inline Q:/A: markers.
        if len(non_empty) == 1:
            cell_text = non_empty[0]
            qa = split_q_and_a(cell_text)

            if qa is not None:
                if pending_q_text is not None:
                    emit(pending_q_text, "")
                    pending_q_text = None

                emit(qa["Q"], qa["A"])
                continue

            has_q = re.search(r"\bQ:\s*", cell_text, re.IGNORECASE)
            has_a = re.search(r"\bA:\s*", cell_text, re.IGNORECASE)

            if has_q and not has_a:
                # Dangling question; hold it for the answer row that
                # should follow (e.g. split across a page break).
                if pending_q_text is not None:
                    emit(pending_q_text, "")

                pending_q_text = cell_text[has_q.end() :]
                continue

            if has_a and pending_q_text is not None:
                emit(pending_q_text, cell_text[has_a.end() :])
                pending_q_text = None
                continue

            # A non-Q&A stray cell inside the QA table; nothing to pair.
            continue

        # -- Exactly 2 populated columns. This can mean either:
        #      [question text, answer text]   -- true 2-column layout
        #      [row number, "Q: ... A: ..."]  -- a numbered row whose
        #                                         second cell already
        #                                         contains BOTH inline
        #                                         markers combined
        #    Inline markers on either cell must be checked first, or a
        #    numbered row gets its number treated as the question and
        #    the whole "Q: ... A: ..." string treated as the answer.
        if len(non_empty) == 2:
            col_a, col_b = non_empty[0], non_empty[1]
            key_candidate = clean_cell_text(col_a)

            qa_from_b = split_q_and_a(col_b)
            qa_from_a = split_q_and_a(col_a)

            if qa_from_b is not None and re.fullmatch(r"\d+", key_candidate):
                # e.g. ["1", "Q: ... A: ..."] -- number is the key,
                # second cell is the full inline pair.
                emit(qa_from_b["Q"], qa_from_b["A"], key=key_candidate)
                continue

            if qa_from_a is not None:
                # First cell already holds the full inline pair.
                emit(qa_from_a["Q"], qa_from_a["A"])
                continue

            if qa_from_b is not None:
                # Second cell holds the full inline pair, first cell
                # is some other label (not a bare row number).
                emit(qa_from_b["Q"], qa_from_b["A"])
                continue

            # Neither cell has a COMPLETE inline "Q: ... A: ..." pair,
            # but one of them may still carry a bare "Q:" marker with
            # no answer -- e.g. ["1", "Q: text that continues on the
            # next page"]. That is a dangling question split across a
            # page break, not a genuine two-column [question, answer]
            # row, and must not be emitted as a complete pair (it
            # would silently become {"Q": "1", "A": "Q: text..."}).
            state_a = _row_dangling_qa_state([col_a])
            state_b = _row_dangling_qa_state([col_b])

            if "dangling_q" in (state_a, state_b):
                dangling_cell = col_a if state_a == "dangling_q" else col_b
                has_q = re.search(r"\bQ:\s*", dangling_cell, re.IGNORECASE)

                if pending_q_text is not None:
                    emit(pending_q_text, "")

                pending_q_text = dangling_cell[has_q.end() :]
                continue

            if "bare_a" in (state_a, state_b) and pending_q_text is not None:
                bare_cell = col_a if state_a == "bare_a" else col_b
                has_a = re.search(r"\bA:\s*", bare_cell, re.IGNORECASE)
                emit(pending_q_text, bare_cell[has_a.end() :])
                pending_q_text = None
                continue

            # No inline markers anywhere -- a genuine two-column
            # [question, answer] table; the row IS the pair.
            emit(col_a, col_b)
            continue

        # -- 3+ columns: first column is usually a row number.
        if len(row) >= 3:
            key_candidate = clean_cell_text(row[0])
            rest = [cell for cell in row[1:] if cell]

            if key_candidate and re.fullmatch(r"\d+", key_candidate) and len(rest) >= 2:
                emit(rest[0], " ".join(rest[1:]), key=key_candidate)
                continue

            value_text = " ".join(rest)
            qa = split_q_and_a(value_text)

            if qa is not None:
                emit(qa["Q"], qa["A"], key=key_candidate or None)
                continue

    if pending_q_text is not None:
        emit(pending_q_text, "")

    return pairs


def table_rows_to_structured_content(
    rows,
    title=None,
):
    """
    Generic table -> nested JSON.

    Standard 2+ column table:
        first column = key
        remaining columns = value

    Q&A table:
        question number = key
        value = {"Q": ..., "A": ...}

    One-column table:
        each row is retained as a value.
    """
    rows = normalize_table_rows(rows)

    if not rows:
        return {
            "type": "table",
            "content": [],
        }

    detected_title = table_title_from_rows(rows)

    # When this table lives inside (or is titled) a "Questions and
    # Answers" section, inspect the PyMuPDF grid directly so each
    # table row becomes its own Q&A pair, rather than falling back to
    # the generic key/value or regex-based reconstruction below.
    if _is_qa_section_title(title) or _is_qa_section_title(detected_title):
        qa_pairs = parse_qa_grid_rows(rows)

        if qa_pairs:
            return {
                "type": "table",
                "content": qa_pairs,
            }

    if title and detected_title and heading_text_matches(detected_title, title):
        rows = rows[1:]

    structured = []

    # Pre-pass: merge rows where Q: appears without a matching A:
    # (happens when the Q heading is on one page and A starts on the next)
    merged_rows = []
    pending_row = None

    for row in rows:
        if not any(row):
            continue

        if len(row) >= 2:
            value_text = clean_cell_text(" ".join(cell for cell in row[1:] if cell))
        elif len(row) == 1:
            value_text = clean_cell_text(row[0])
        else:
            value_text = ""

        if pending_row is not None:
            # Try to complete a dangling Q: with this row's text
            if len(pending_row) >= 2:
                pending_val = clean_cell_text(" ".join(c for c in pending_row[1:] if c))
            else:
                pending_val = clean_cell_text(pending_row[0]) if pending_row else ""

            combined = pending_val + " " + value_text
            if split_q_and_a(combined) is not None:
                # Successfully merged — build a synthetic merged row
                if len(pending_row) >= 2:
                    merged = [pending_row[0], combined]
                else:
                    merged = [combined]
                merged_rows.append(merged)
                pending_row = None
                continue
            else:
                # Cannot extend further — emit the pending row as-is
                merged_rows.append(pending_row)
                pending_row = None

        # Check whether this row has Q: but no A: (dangling question)
        q_hit = re.search(r"\bQ:\s*", value_text, re.IGNORECASE)
        a_hit = re.search(r"\bA:\s*", value_text, re.IGNORECASE)
        if q_hit and not a_hit:
            pending_row = row
        else:
            merged_rows.append(row)

    if pending_row is not None:
        merged_rows.append(pending_row)

    for row in merged_rows:
        if not any(row):
            continue

        if len(row) >= 2:
            key = clean_cell_text(row[0])

            value_text = clean_cell_text(" ".join(cell for cell in row[1:] if cell))

            qa = split_q_and_a(value_text)

            if key and re.fullmatch(r"\d+", key) and qa is not None:
                structured.append(
                    {
                        "key": key,
                        "value": qa,
                    }
                )
                continue

            if key and value_text:
                structured.append(
                    {
                        "key": key,
                        "value": value_text,
                    }
                )

        elif len(row) == 1 and row[0]:
            structured.append(row[0])

    return {
        "type": "table",
        "content": structured,
    }


def _table_first_line_index(lines, table):
    """Map a table's top edge to the first extracted PDF line in/at it."""
    page = table["page"]
    x0, y0, x1, y1 = table["bbox"]

    candidates = [
        line
        for line in lines
        if line.get("page") == page
        and "bbox" in line
        and pymupdf.Rect(line["bbox"]).intersects(pymupdf.Rect(x0, y0, x1, y1))
    ]

    if candidates:
        return min(
            candidates,
            key=lambda line: (
                line.get("y", 0),
                line.get("x", 0),
            ),
        )["index"]

    # Fallback when table text is not represented by a normal line.
    candidates = [
        line
        for line in lines
        if line.get("page") == page and line.get("y", 0) >= y0 - 2
    ]

    if candidates:
        return min(
            candidates,
            key=lambda line: (
                line.get("y", 0),
                line.get("x", 0),
            ),
        )["index"]

    return None


def find_table_for_heading(
    tables,
    heading,
    title,
):
    """Backward-compatible single-table lookup.

    Exact title matching is preferred. Untitled fallback is restricted
    to a table that actually occurs after this heading on the same page.
    The old nearest-table logic could steal a Definitions table for
    Reimbursement Guidelines.
    """
    titled = []

    for table in tables:
        if table["page"] != heading["page"]:
            continue

        table_title = table_title_from_rows(table["rows"])

        if table_title and heading_text_matches(table_title, title):
            titled.append(table)

    if titled:
        return min(titled, key=lambda t: abs(t["bbox"][1] - heading["y"]))

    untitled = [
        table
        for table in tables
        if (
            table["page"] == heading["page"]
            and table_title_from_rows(table["rows"]) is None
            and table["bbox"][1] >= heading["y"] - 5
        )
    ]

    if not untitled:
        return None

    return min(untitled, key=lambda t: max(0, t["bbox"][1] - heading["y"]))


def attach_tables_to_nodes(
    tree,
    tables,
    lines=None,
):
    """
    Assign EVERY detected table to the section that owns its reading
    position.

    Rules:
      1. An explicit table title wins.
      2. Otherwise the owner is the latest semantic heading before the
         table in PDF reading order, including headings on a previous page.
      3. A node can own multiple tables.
      4. A table is attached exactly once.

    This prevents a Definitions table from being pulled into the
    preceding Reimbursement Guidelines section.
    """
    flat = flatten_toc_tree(tree)

    if lines is None:
        # Keep compatibility with older callers.
        lines = []

    for node in flat:
        node.pop("table", None)
        node.pop("tables", None)
        node.pop("_table_bbox", None)
        node.pop("_table_page", None)

    if not tables:
        return flat

    # Nodes that have real headings, sorted in document order.
    headed_nodes = [node for node in flat if node.get("heading") is not None]
    headed_nodes.sort(key=lambda node: node["heading"]["index"])

    for table in tables:
        owner = None
        table_title = table_title_from_rows(table["rows"])

        # --------------------------------------------------------
        # 1. Explicit table title -> exact semantic heading.
        # --------------------------------------------------------
        if table_title:
            matches = [
                node
                for node in headed_nodes
                if heading_text_matches(table_title, node.get("title", ""))
            ]

            if matches:
                # Prefer the latest matching heading before the table.
                table_index = _table_first_line_index(lines, table) if lines else None

                if table_index is not None:
                    before = [
                        node
                        for node in matches
                        if node["heading"]["index"] <= table_index
                    ]
                    if before:
                        owner = before[-1]

                if owner is None:
                    owner = matches[-1]

        # --------------------------------------------------------
        # 2. Untitled table -> latest heading before table.
        # --------------------------------------------------------
        if owner is None:
            table_index = _table_first_line_index(lines, table) if lines else None

            if table_index is not None:
                candidates = [
                    node
                    for node in headed_nodes
                    if node["heading"]["index"] <= table_index
                ]

                if candidates:
                    owner = candidates[-1]

        if owner is None:
            continue

        structured = table_rows_to_structured_content(
            table["rows"],
            title=owner["title"],
        )

        if not structured["content"]:
            continue

        owner.setdefault("tables", []).append(
            {
                "bbox": table["bbox"],
                "page": table["page"],
                "content": structured,
            }
        )

    # Compatibility: keep the old singular "table" representation
    # when a node has exactly one table.
    for node in headed_nodes:
        node_tables = node.get("tables", [])

        if len(node_tables) == 1:
            table = node_tables[0]
            node["table"] = {
                "type": table["content"]["type"],
                "content": table["content"]["content"],
            }
            node["_table_bbox"] = table["bbox"]
            node["_table_page"] = table["page"]

        elif len(node_tables) > 1:
            # Retain geometry for all tables. node_to_json will use
            # the multi-table path.
            node["_table_bbox"] = None
            node["_table_page"] = None

    return flat


# ============================================================
# BOLD SUBHEADINGS
# ============================================================


def resolve_bold_subheadings(
    lines,
    node,
):
    if normalize_heading_text(node.get("title", "")) in NON_SUBSECTION_PARENT_TITLES:
        node["children"] = []
        node.setdefault("own_content", node.get("content", []))
        return

    heading = node.get("heading")
    children = node.get("children", [])

    if heading is None:
        return

    if not children:
        node.setdefault("own_content", node.get("content", []))
        return

    parent_start = heading["index"] + 1

    parent_end = (
        node["next_index"] if node.get("next_index") is not None else len(lines)
    )

    confirmed = []

    for child in children:
        if child.get("match_type") == "blue":
            confirmed.append(child)
            continue

        occurrences = find_target_occurrences(
            lines,
            child["title"],
            parent_start,
            parent_end,
        )

        bold = [
            occurrence
            for occurrence in occurrences
            if (occurrence["bold"] and not occurrence["blue"])
        ]

        if bold:
            child["heading"] = bold[0]
            child["match_type"] = "bold"
            confirmed.append(child)

    confirmed.sort(key=lambda c: c["heading"]["index"])

    for i, child in enumerate(confirmed):
        start = child["heading"]["index"]

        end = (
            confirmed[i + 1]["heading"]["index"]
            if i + 1 < len(confirmed)
            else parent_end
        )

        child["content"] = lines[start + 1 : end]

        child["own_content"] = child["content"]

        child["next_index"] = end

    if confirmed:
        first_child = confirmed[0]["heading"]["index"]

        node["own_content"] = lines[parent_start:first_child]

    for child in children:
        resolve_bold_subheadings(lines, child)


# ============================================================
# REIMBURSEMENT GUIDELINES
# ============================================================

_CODE_TOKEN_PATTERN = re.compile(r"^\d{4,5}[A-Z]?$")

_MODIFIER_TOKEN_PATTERN = re.compile(r"^[A-Z0-9]{1,2}(-[A-Z0-9]{1,2})?$")

_CONDITION_ITEM_PATTERN = re.compile(r"^[a-zA-Z]\.\s+")


def is_code_line(text):
    tokens = text.split()

    return bool(tokens) and all(_CODE_TOKEN_PATTERN.match(token) for token in tokens)


def is_modifier_line(text):
    tokens = text.split()

    return bool(tokens) and all(
        _MODIFIER_TOKEN_PATTERN.match(token) for token in tokens
    )


def is_condition_item_line(text):
    return bool(_CONDITION_ITEM_PATTERN.match(text.strip()))


def is_subsection_heading_for_guidelines(
    line,
    tables=None,
):
    if not line["bold"] or line["blue"]:
        return False

    # Table headers/cells are data, never semantic subsections.
    # This is especially important for "CPT Codes", "MFD Value",
    # "Term", "Definition", etc. inside reimbursement/definition tables.
    if line_in_any_table(line, tables or []):
        return False

    text = line["text"].strip()

    if not text:
        return False

    normalized = normalize_heading_text(text)

    if (
        normalized in PROCEDURE_CODE_LABELS
        or normalized in CONDITION_LABELS
        or normalized in MODIFIER_LABELS
    ):
        return False

    if is_code_line(text) or is_modifier_line(text) or is_condition_item_line(text):
        return False

    text_clean = text.rstrip(".").strip()
    if len(text) > 100:
        return False

    if (
        text.endswith(".")
        and len(text_clean.split()) > 10
        and not any(k in normalized for k in ["reimbursement", "guideline", "overview"])
    ):
        return False

    return True


def parse_reimbursement_subsections(
    lines,
    tables=None,
):
    heading_indices = [
        index
        for index, line in enumerate(lines)
        if is_subsection_heading_for_guidelines(
            line,
            tables=tables,
        )
    ]

    subsections = []

    for position, start in enumerate(heading_indices):
        end = (
            heading_indices[position + 1]
            if position + 1 < len(heading_indices)
            else len(lines)
        )

        heading_line = lines[start]

        subsection = {
            "title": heading_line["text"].strip(),
            "content": [],
            "procedure_codes": [],
            "conditions": [],
            "modifiers": [],
        }

        current_label = None

        for line in lines[start + 1 : end]:
            text = line["text"].strip()

            if not text:
                continue

            normalized = normalize_heading_text(text)

            if normalized in PROCEDURE_CODE_LABELS:
                current_label = "procedure_codes"
                continue

            if normalized in CONDITION_LABELS:
                current_label = "conditions"
                continue

            if normalized in MODIFIER_LABELS:
                current_label = "modifiers"
                continue

            if current_label == "procedure_codes" and is_code_line(text):
                subsection["procedure_codes"].extend(text.split())
                continue

            if current_label == "modifiers" and is_modifier_line(text):
                subsection["modifiers"].extend(text.split())
                continue

            if current_label == "conditions":
                if is_condition_item_line(text) or not subsection["conditions"]:
                    subsection["conditions"].append(text)
                else:
                    subsection["conditions"][-1] += " " + text

                continue

            subsection["content"].append(text)

        subsections.append(subsection)

    return subsections


# ============================================================
# POLICY METADATA
# ============================================================

POLICY_NUM_LABEL_REGEX = re.compile(
    r"Policy\s*Number\s*[:\s]*([A-Za-z0-9\.\-]+)", re.IGNORECASE
)

POLICY_NUM_CODE_REGEX = re.compile(r"\b\d{4}[A-Z]\d{4}[A-Z]?\b")


def extract_policy_title(fallback_basename):
    """
    Policy title is taken directly from the PDF filename.
    """
    with open(CSV_PATH, newline="", encoding="utf-16") as f:
        reader = csv.DictReader(f)
        for row in reader:
            index = row["index"].strip()
            original_title = row["policy_title"].strip()
            if str(fallback_basename) == str(index):
                return original_title
    return None


def extract_policy_number(raw_lines):
    for line in raw_lines[:60]:
        text = line["text"].strip()

        match = POLICY_NUM_LABEL_REGEX.search(text)

        if match:
            return match.group(1)

        match = POLICY_NUM_CODE_REGEX.search(text)

        if match:
            return match.group(0)

    return None


# ============================================================
# POLICY DATES
# ============================================================

EFFECTIVE_DATE_LABELS = (
    "original effective date",
    "effective date",
    "publication date",
    "published date",
    "publish date",
    "issue date",
    "approval date",
)

EXPIRY_DATE_LABELS = (
    "expiration date",
    "expiry date",
    "termination date",
    "end date",
)

_MONTH_NAMES = (
    r"January|February|March|April|May|June|July|"
    r"August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)

_DATE_VALUE_PATTERN = re.compile(
    r"(?:\d{1,2}/\d{1,2}/\d{2,4})"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:(?:" + _MONTH_NAMES + r")\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def extract_date_value(text):
    match = _DATE_VALUE_PATTERN.search(text)
    return match.group(0) if match else None


def find_label_in_text(
    text_lower,
    labels,
):
    for label in labels:
        if label in text_lower:
            return label

    return None


def find_dates_from_front_matter_labels(
    lines,
    max_scan_lines=400,
):
    result = {
        "effective_date": None,
        "expiry_date": None,
    }

    scan_end = min(len(lines), max_scan_lines)

    date_groups = (
        ("effective_date", EFFECTIVE_DATE_LABELS),
        ("expiry_date", EXPIRY_DATE_LABELS),
    )

    for index in range(scan_end):
        line = lines[index]

        text_lower = line["text"].strip().lower()

        for key, labels in date_groups:
            if result[key] is not None:
                continue

            matched_label = find_label_in_text(text_lower, labels)

            if matched_label is None:
                continue

            date_value = extract_date_value(line["text"])

            source_index = index

            if date_value is None and index + 1 < len(lines):
                candidate = extract_date_value(lines[index + 1]["text"])

                if candidate:
                    date_value = candidate
                    source_index = index + 1

            if date_value:
                result[key] = {
                    "value": date_value,
                    "label": matched_label,
                    "page": lines[source_index]["page"],
                    "index": source_index,
                }

    return result


EXPIRY_HISTORY_KEYWORDS = (
    "expired",
    "expiration",
    "terminated",
    "termination",
    "retired",
    "discontinued",
    "no longer in effect",
    "superseded",
)


def group_lines_into_rows(
    content_lines,
    y_tolerance=6.0,
):
    rows = []

    for line in content_lines:
        placed = False

        for row in rows:
            if (
                row[0].get("page") == line.get("page")
                and abs(row[0]["y"] - line["y"]) <= y_tolerance
            ):
                row.append(line)
                placed = True
                break

        if not placed:
            rows.append([line])

    rows.sort(key=lambda row: (row[0].get("page", 0), row[0]["y"]))

    for row in rows:
        row.sort(key=lambda cell: cell["x"])

    return rows


def parse_history_table_from_lines(lines):
    heading = resolve_heading_occurrence(lines, "History", verbose=False)

    if heading is None:
        return []

    content, _ = extract_heading_content(lines, heading)

    rows = group_lines_into_rows(content)

    parsed = []

    for row in rows:
        date_value = None
        description = []

        for cell in row:
            text = cell["text"].strip()

            candidate = extract_date_value(text)

            if date_value is None and candidate and candidate == text:
                date_value = candidate
                continue

            description.append(text)

        if date_value is None:
            for cell in row:
                candidate = extract_date_value(cell["text"])

                if candidate:
                    date_value = candidate
                    break

        if date_value:
            parsed.append(
                {
                    "date": date_value,
                    "description": " ".join(description).strip(),
                    "page": row[0]["page"],
                }
            )

    return parsed


def find_dates_from_history_table(lines):
    result = {
        "effective_date": None,
        "expiry_date": None,
    }

    rows = parse_history_table_from_lines(lines)

    if not rows:
        return result

    top = rows[0]

    result["effective_date"] = {
        "value": top["date"],
        "label": (top["description"] or "History"),
        "page": top["page"],
        "index": None,
    }

    for row in rows:
        description = row["description"].lower()

        if any(keyword in description for keyword in EXPIRY_HISTORY_KEYWORDS):
            result["expiry_date"] = {
                "value": row["date"],
                "label": row["description"],
                "page": row["page"],
                "index": None,
            }
            break

    return result


def find_policy_dates(lines):
    result = find_dates_from_front_matter_labels(lines)

    history = find_dates_from_history_table(lines)

    for key in (
        "effective_date",
        "expiry_date",
    ):
        if result[key] is None:
            result[key] = history[key]

    return result


# ============================================================
# JSON SERIALIZATION
# ============================================================


def subsection_to_json(subsection):
    result = {"title": subsection["title"]}

    if subsection["content"]:
        result["content"] = " ".join(subsection["content"])

    if subsection["procedure_codes"]:
        result["procedure_codes"] = subsection["procedure_codes"]

    if subsection["conditions"]:
        result["conditions"] = subsection["conditions"]

    if subsection["modifiers"]:
        result["modifiers"] = subsection["modifiers"]

    return result


def _build_mixed_section_content(node):
    """
    Preserve normal text AND every table in true PDF reading order.

    Tables are never converted to strings here. Their structured rows
    remain dictionaries, so downstream extraction_rules generation can
    preserve them instead of collapsing them into prose.
    """
    norm_title = normalize_heading_text(node.get("title", ""))
    raw_tables = node.get("tables", [])

    # Backward compatibility for older in-memory nodes.
    if not raw_tables and node.get("table"):
        raw_tables = [
            {
                "bbox": node.get("_table_bbox"),
                "page": node.get("_table_page"),
                "content": node.get("table"),
            }
        ]

    if norm_title in ("questions and answers", "definitions", "history") and raw_tables:
        mixed = []
        for table in raw_tables:
            tcontent = table.get("content", {})
            if isinstance(tcontent, dict) and "content" in tcontent:
                mixed.extend(tcontent["content"])
            elif isinstance(tcontent, list):
                mixed.extend(tcontent)
        if mixed:
            return mixed

    text_lines = node.get("own_content", node.get("content", []))

    if not isinstance(text_lines, list):
        text_lines = []

    raw_tables = node.get("tables", [])

    # Backward compatibility for older in-memory nodes.
    if not raw_tables and node.get("table"):
        raw_tables = [
            {
                "bbox": node.get("_table_bbox"),
                "page": node.get("_table_page"),
                "content": node.get("table"),
            }
        ]

    table_rects = []
    for table in raw_tables:
        bbox = table.get("bbox")
        page = table.get("page")
        if bbox:
            table_rects.append(
                (
                    page,
                    pymupdf.Rect(bbox),
                )
            )

    outside = []

    for line in text_lines:
        if not isinstance(line, dict):
            continue

        line_rect = pymupdf.Rect(line["bbox"]) if "bbox" in line else None

        inside_table = False

        if line_rect:
            for table_page, table_rect in table_rects:
                if line.get("page") == table_page and line_rect.intersects(table_rect):
                    inside_table = True
                    break

        if not inside_table:
            outside.append(line)

    blocks = []

    outside.sort(
        key=lambda x: (
            x.get("page", 0),
            x.get("y", 0),
            x.get("x", 0),
        )
    )

    current = []
    previous = None

    for line in outside:
        if previous is None:
            current = [line]
        else:
            same_page = line.get("page") == previous.get("page")
            y_gap = line.get("y", 0) - previous.get("y", 0)

            if not same_page or y_gap > max(
                previous.get("font", 10) * 1.8,
                12,
            ):
                if current:
                    blocks.append(
                        {
                            "kind": "text",
                            "page": current[0].get("page", 0),
                            "y": current[0].get("y", 0),
                            "value": join_lines(current),
                        }
                    )
                current = [line]
            else:
                current.append(line)

        previous = line

    if current:
        blocks.append(
            {
                "kind": "text",
                "page": current[0].get("page", 0),
                "y": current[0].get("y", 0),
                "value": join_lines(current),
            }
        )

    for table in raw_tables:
        bbox = table.get("bbox")
        blocks.append(
            {
                "kind": "table",
                "page": table.get("page", 0),
                "y": bbox[1] if bbox else 0,
                "content": table.get("content", {}).get("content", []),
            }
        )

    blocks.sort(
        key=lambda x: (
            x.get("page", 0),
            x.get("y", 0),
        )
    )

    mixed = []

    for block in blocks:
        if block["kind"] == "text":
            if block["value"].strip():
                mixed.append(block["value"].strip())
        else:
            mixed.extend(block["content"])

    return mixed


def node_to_json(node):
    clean_title = normalize_unicode_punctuation(node["title"]).strip()
    result = {
        "title": clean_title,
        "level": node.get("level", 1),
        "confirmed": bool(node.get("heading") or node.get("confirmed")),
    }

    if node.get("heading"):
        result["page"] = node["heading"]["page"]
        result["matched_via"] = node.get("match_type", "blue")

    content = node.get("own_content", node.get("content", []))

    # IMPORTANT:
    # A section can be a mixture of paragraphs + table.
    # Never let the table replace the section's text.
    if node.get("tables") or node.get("table"):
        result["type"] = "mixed"
        result["content"] = _build_mixed_section_content(node)
    else:
        result["type"] = "text"

        if isinstance(content, list):
            result["content"] = normalize_unicode_punctuation(
                join_lines(content)
            ).strip()
        else:
            result["content"] = normalize_unicode_punctuation(
                str(content or "")
            ).strip()

    if node.get("subsections"):
        result["subsections"] = [
            subsection_to_json(subsection) for subsection in node["subsections"]
        ]

    if node.get("children"):
        result["children"] = [node_to_json(child) for child in node["children"]]

    return result


def build_json_export(
    policy_title,
    policy_number,
    policy_dates,
    tree,
    linked_pdf_contents=None,
):
    return {
        "policy_title": policy_title,
        "policy_number": policy_number,
        "policy_dates": {
            "effective_date": (
                policy_dates["effective_date"]["value"]
                if policy_dates.get("effective_date")
                else None
            ),
            "expiry_date": (
                policy_dates["expiry_date"]["value"]
                if policy_dates.get("expiry_date")
                else None
            ),
        },
        "sections": [node_to_json(node) for node in tree],
        "linked_pdf_contents": linked_pdf_contents or None,
    }


def write_json_export(
    export_dict,
    pdf_path,
):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    base_name = os.path.splitext(os.path.basename(pdf_path))[0]

    line_of_business = export_dict.get("Line_of_Business")

    if line_of_business == "Medicare Advantage":
        lob_suffix = "medicare_advantage"

    elif line_of_business == "Commercial":
        lob_suffix = "commercial"

    else:
        lob_suffix = "unknown"

    json_filename = f"{base_name}.json"

    json_path = os.path.join(OUTPUT_DIR, json_filename)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export_dict, f, indent=2, ensure_ascii=False)

    return json_path


def determine_line_of_business(pdf_path, raw_lines=None):
    normalized_path = os.path.normpath(pdf_path).lower()

    if "medicare_advantage" in normalized_path:
        return "Medicare Advantage"

    if "commercial" in normalized_path:
        return "Commercial"

    if raw_lines:
        for line in raw_lines[:25]:
            text = line.get("text", "").lower()
            if "medicare advantage" in text:
                return "Medicare Advantage"
            if "commercial" in text:
                return "Commercial"

    return None


def determine_billing_class(policy_title, raw_lines=None, doc=None):
    """
    Determine billing class from the policy title, front matter, or Application section.
    """
    title = (policy_title or "").lower()

    has_prof_in_title = "professional" in title
    has_fac_in_title = "facility" in title

    if has_prof_in_title and has_fac_in_title:
        return "Both"

    if has_prof_in_title:
        return "Professional"

    if has_fac_in_title:
        return "Facility"

    doc_text = ""
    if raw_lines:
        doc_text = " ".join(line.get("text", "") for line in raw_lines[:80])
    elif doc:
        for p in doc[:2]:
            doc_text += p.get_text("text") + " "

    doc_text_norm = doc_text.lower()

    has_cms1500 = (
        "1500 health insurance claim form" in doc_text_norm
        or "cms 1500" in doc_text_norm
        or "cms-1500" in doc_text_norm
    )
    has_ub04 = (
        "ub04" in doc_text_norm
        or "ub-04" in doc_text_norm
        or "cms 1450" in doc_text_norm
        or "cms-1450" in doc_text_norm
    )

    has_prof_word = "professional" in doc_text_norm
    has_fac_word = "facility" in doc_text_norm

    if (has_cms1500 and has_ub04) or (
        has_prof_word
        and has_fac_word
        and (
            "professional and facility" in doc_text_norm
            or "professional and/or facility" in doc_text_norm
            or "professional/facility" in doc_text_norm
        )
    ):
        return "Both"

    if has_cms1500 or (has_prof_word and not has_fac_word):
        return "Professional"

    if has_ub04 or (has_fac_word and not has_prof_word):
        return "Facility"

    return None


def linked_table_rows_to_records(rows):
    """
    Convert a linked-PDF table's raw rows into column-name -> value
    records, using the table's first row as the header.

    Linked PDFs (e.g. NDC/HCPCS code lists) are typically genuine
    multi-column data tables rather than the 2-column key/value tables
    found in the main policy document, so each row is kept as a
    {column_name: value, ...} record instead of being collapsed into a
    single "key" + concatenated "value" string.
    """
    rows = normalize_table_rows(rows)

    if not rows:
        return {"columns": [], "rows": []}

    header_row = rows[0]
    data_rows = rows[1:]

    column_count = max(len(row) for row in rows)

    columns = []
    seen_columns = {}

    for idx in range(column_count):
        raw_name = header_row[idx].strip() if idx < len(header_row) else ""
        name = raw_name if raw_name else f"Column {idx + 1}"

        # De-duplicate repeated header names (e.g. two "Max Unit" columns)
        if name in seen_columns:
            seen_columns[name] += 1
            name = f"{name} {seen_columns[name]}"
        else:
            seen_columns[name] = 1

        columns.append(name)

    records = []

    for row in data_rows:
        if not any(row):
            continue

        record = {
            column_name: (row[idx] if idx < len(row) else "")
            for idx, column_name in enumerate(columns)
        }

        records.append(record)

    return {"columns": columns, "rows": records}


def extract_linked_pdf_contents(doc):
    """
    Extract ALL contents from PDF hyperlinks embedded in the policy.

    For every linked PDF:
        - Extract normal text
        - Extract tables
        - Preserve table rows as key/value where possible
        - Extract CPT/HCPCS codes
        - Extract ICD codes
        - Keep each linked PDF as a separate object

    Nothing from the linked PDF is discarded just because it is
    not a CPT or ICD code.
    """

    linked_documents = []

    processed_urls = set()

    for page in doc:

        try:
            links = page.get_links()
        except Exception:
            continue

        for link in links:

            uri = link.get("uri")

            if not uri:
                continue

            uri = uri.strip()

            if not uri.lower().startswith(("http://", "https://")):
                continue

            uri_lower = uri.lower().split("?")[0]

            # Avoid processing the same linked PDF more than once.
            if uri in processed_urls:
                continue

            # Only request likely document links.
            is_doc_candidate = (
                uri_lower.endswith(".pdf")
                or "attachment" in uri_lower
                or "policy" in uri_lower
                or "code" in uri_lower
                or "document" in uri_lower
                or "download" in uri_lower
            )

            if not is_doc_candidate:
                continue

            processed_urls.add(uri)

            try:

                response = requests.get(uri, timeout=10)

                if response.status_code != 200:
                    continue

                content_type = response.headers.get("Content-Type", "").lower()

                is_pdf = (
                    "application/pdf" in content_type
                    or uri_lower.endswith(".pdf")
                    or response.content[:4] == b"%PDF"
                )

                if not is_pdf:
                    continue

                # Open linked PDF directly from memory.
                linked_doc = pymupdf.open(stream=response.content, filetype="pdf")

                document_text = []
                document_tables = []

                linked_cpt_codes = []
                linked_icd_codes = []

                # ------------------------------------------------
                # PROCESS EVERY PAGE
                # ------------------------------------------------

                for page_number, linked_page in enumerate(linked_doc, start=1):

                    # --------------------------------------------
                    # 1. NORMAL TEXT
                    # --------------------------------------------

                    page_text = linked_page.get_text("text").strip()

                    if page_text:
                        document_text.append(
                            {"page": page_number, "content": page_text}
                        )

                    # --------------------------------------------
                    # 2. CPT + ICD CODES
                    # --------------------------------------------

                    for code in re.findall(CPT_CODE_REGEX, page_text):

                        if code not in linked_cpt_codes:
                            linked_cpt_codes.append(code)

                    for icd_code in re.findall(ICD_CODE_REGEX, page_text):

                        if icd_code not in linked_icd_codes:
                            linked_icd_codes.append(icd_code)

                    # --------------------------------------------
                    # 3. TABLES
                    # --------------------------------------------

                    page_tables = []

                    # First use PyMuPDF's native table detector.
                    if hasattr(linked_page, "find_tables"):

                        try:

                            finder = linked_page.find_tables()

                            page_tables = getattr(finder, "tables", []) or []

                        except Exception:
                            page_tables = []

                    for table in page_tables:

                        try:
                            rows = table.extract()
                        except Exception:
                            rows = []

                        if not rows:
                            continue

                        normalized_rows = normalize_table_rows(rows)

                        if not normalized_rows:
                            continue

                        structured_table = linked_table_rows_to_records(
                            normalized_rows
                        )

                        if not structured_table.get("rows"):
                            continue

                        document_tables.append(
                            {
                                "page": page_number,
                                "columns": structured_table.get("columns", []),
                                "rows": structured_table.get("rows", []),
                            }
                        )

                linked_doc.close()

                # ------------------------------------------------
                # STORE COMPLETE LINKED DOCUMENT
                # ------------------------------------------------

                linked_documents.append(
                    {
                        "url": uri,
                        "content": document_text,
                        "tables": document_tables,
                        "CPT_CODES_USED": (linked_cpt_codes),
                        "ICD_CODES_USED": (linked_icd_codes),
                    }
                )

            except Exception as exc:

                print(f"Could not process linked PDF: {uri}")

                print(f"Reason: {exc}")

    return linked_documents


def process_pdf(pdf_path):

    doc = pymupdf.open(pdf_path)

    # 1. Raw PDF extraction.
    raw_lines = extract_lines(doc)

    # Extract CPT codes directly from the already extracted PDF lines.
    # Keep first-occurrence order and remove duplicates.
    cpt_codes_used = []
    icd_codes_used = []
    modifiers_used_here = []

    for line in raw_lines:
        text = line.get("text", "")

        for code in re.findall(
            CPT_CODE_REGEX,
            text,
        ):
            if code not in cpt_codes_used:
                cpt_codes_used.append(code)

        for icd_code in re.findall(
            ICD_CODE_REGEX,
            text,
        ):
            if icd_code not in icd_codes_used:
                icd_codes_used.append(icd_code)

        # Collect modifier codes only from lines that mention "modifier"
        # within a short context window to avoid false positives.
        if contains_modifier(text):
            for mod in MODIFIER_EXTRACT_REGEX.findall(text):
                mod_upper = mod.upper()
                if mod_upper not in modifiers_used_here:
                    modifiers_used_here.append(mod_upper)

    linked_pdf_contents = extract_linked_pdf_contents(doc)

    # 2. Metadata.
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]

    policy_title = extract_policy_title(base_name)

    # Policy-level classification.
    line_of_business = determine_line_of_business(
        pdf_path,
    )

    billing_class = determine_billing_class(
        policy_title,
        raw_lines=raw_lines,
        doc=doc,
    )

    policy_number = extract_policy_number(raw_lines)

    policy_dates = find_policy_dates(raw_lines)

    # 3. Remove repeated headers/footers.
    lines = clean_lines(raw_lines, doc.page_count)

    # 4. Detect REAL tables before constructing JSON.
    tables = detect_tables(doc)

    # 5. TOC / no-TOC.
    toc_start = find_toc_start_index(lines)

    toc_entries = []

    if toc_start is not None:
        toc_entries = extract_toc_entries(lines, toc_start)

    if toc_entries:
        toc_tree = build_toc_tree(toc_entries)

        flat_nodes = flatten_toc_tree(toc_tree)

        toc_end_index = (
            max(e.get("toc_line_index", toc_start) for e in toc_entries)
            if toc_entries
            else toc_start
        )

        for node in flat_nodes:
            heading = resolve_heading_occurrence(
                lines,
                node["title"],
                search_start=toc_end_index + 1,
            )

            if heading is not None:
                node["heading"] = heading
                node["match_type"] = "blue"

                content, next_index = extract_heading_content(lines, heading)

                node["content"] = content
                node["own_content"] = content
                node["next_index"] = next_index

        for root in toc_tree:
            resolve_bold_subheadings(lines, root)

        # Check if Questions and Answers was omitted from # printed TOC but exists in body
        has_qa = any(
            normalize_heading_text(n.get("title", ""))
            in ("questions and answers", "question and answers")
            for n in flat_nodes
        )
        if not has_qa:
            qa_heading = resolve_heading_occurrence(
                lines, "Questions and Answers", search_start=toc_end_index + 1
            )
            if qa_heading:
                content, next_index = extract_heading_content(lines, qa_heading)
                qa_node = {
                    "title": "Questions and Answers",
                    "level": 1,
                    "confirmed": True,
                    "page": qa_heading["page"],
                    "matched_via": "blue" if qa_heading.get("blue") else "bold",
                    "match_type": "blue" if qa_heading.get("blue") else "bold",
                    "heading": qa_heading,
                    "content": content,
                    "own_content": content,
                    "next_index": next_index,
                    "children": [],
                }
                toc_tree.append(qa_node)
                flat_nodes.append(qa_node)

    else:
        toc_tree, flat_nodes = build_tree_without_toc(lines, tables=tables)

    # 6. Attach detected tables to the correct
    #    blue section using the actual table title.
    flat_nodes = attach_tables_to_nodes(
        toc_tree,
        tables,
        lines=lines,
    )

    # 7. Preserve the original specialized
    #    Reimbursement Guidelines parser.
    #    Also recurse into children of the RG node so that
    #    "Procedure Code" sub-subsections nested under a bold child
    #    heading are detected correctly (Bug 1 fix).
    def _apply_rg_subsection_parser(node):
        guideline_lines = node.get("own_content", node.get("content", []))

        subsections = parse_reimbursement_subsections(
            guideline_lines,
            tables=tables,
        )

        if subsections:
            node["subsections"] = subsections

        # Recurse into bold children of this RG node
        for child in node.get("children", []):
            _apply_rg_subsection_parser(child)

    for node in flat_nodes:
        node_title = node.get("title", "")

        # Match "Reimbursement Guidelines" as either an exact heading
        # or a heading that starts with it followed by qualifying text
        # (e.g. "Reimbursement Guidelines for Advance Practice Health
        # Care Providers."). A strict `== "reimbursement guidelines"`
        # check misses the latter and lets the whole section fall
        # through into Overview instead of getting its subsections
        # parsed.
        normalized_title = normalize_heading_text(node_title)

        is_rg_heading = heading_text_matches(
            node_title, "Reimbursement Guidelines"
        ) or normalized_title.startswith("reimbursement guidelines")

        if not is_rg_heading:
            continue

        # Reimbursement Guidelines may contain both paragraphs and
        # a table. Parse its subsection structure regardless of the
        # presence of a table.
        _apply_rg_subsection_parser(node)

    # 8. JSON.
    export_dict = build_json_export(
        policy_title,
        policy_number,
        policy_dates,
        toc_tree,
        linked_pdf_contents=linked_pdf_contents,
    )

    # Store policy classifications and CPT/ICD/modifier codes in the
    # individual extracted JSON.
    export_dict["Line_of_Business"] = line_of_business
    export_dict["Billingclass"] = billing_class
    export_dict["CPT_CODES_USED"] = cpt_codes_used
    export_dict["ICD_CODES_USED"] = icd_codes_used
    export_dict["modifiers_used_here"] = modifiers_used_here
    export_dict["linked_pdf_contents"] = linked_pdf_contents

    json_path = write_json_export(export_dict, pdf_path)

    doc.close()

    return json_path


# ============================================================
# BATCH DISCOVERY
# ============================================================


def find_pdf_files(input_dirs):
    """
    Recursively find PDFs under both plan directories.

    Expected structure:
        medicare_advantage_plans/
            Medical_and_Drug/*.pdf
            Reimbursement/*.pdf

        commercial_plans/
            Medical_and_Drug/*.pdf
            Reimbursement/*.pdf

    All PDFs are returned as one flat list. Their original paths are
    retained so Line_of_Business can be determined from the directory.
    """
    pdf_paths = []

    for input_dir in input_dirs:
        if not os.path.isdir(input_dir):
            continue

        for root, _, files in os.walk(input_dir):
            for entry in sorted(files):
                if not entry.lower().endswith(".pdf"):
                    continue

                if DATE_PREFIX_PATTERN.match(entry):
                    continue

                pdf_paths.append(os.path.join(root, entry))

    return sorted(pdf_paths, key=lambda path: path.casefold())


def contains_modifier(
    text,
    window=6,
):
    if not text:
        return False

    words = text.split()

    for i, word in enumerate(words):
        if MODIFIER_WORD_REGEX.fullmatch(word.strip(".,:;()[]{}")):
            start = max(0, i - window)

            end = min(len(words), i + window + 1)

            nearby = " ".join(words[start:end])

            if MODIFIER_CODE_REGEX.search(nearby):
                return True

    return False


def _normalize_for_subsection_match(text):
    if not isinstance(text, str):
        return ""
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _remove_subsection_content_from_parent(parent, subsection):
    """
    Remove subsection content ONLY from the parent content.
    The subsection object itself is never modified.
    """
    if not isinstance(parent, str):
        return parent

    if not isinstance(subsection, str):
        return parent

    subsection = subsection.strip()

    if not subsection:
        return parent

    # Fast/exact path.
    if subsection in parent:
        return parent.replace(subsection, "", 1)

    # Whitespace-normalized fallback.
    parent_norm = _normalize_for_subsection_match(parent)
    subsection_norm = _normalize_for_subsection_match(subsection)

    if not subsection_norm or subsection_norm not in parent_norm:
        return parent

    # Build a whitespace-tolerant pattern from the subsection text.
    tokens = re.split(r"\s+", subsection_norm)
    pattern = r"\s+".join(re.escape(token) for token in tokens)

    try:
        return re.sub(
            pattern,
            " ",
            parent,
            count=1,
            flags=re.IGNORECASE,
        )
    except re.error:
        return parent


def _find_source_pdf_for_json(
    json_path,
    source_pdf_map=None,
):
    """
    Locate the source PDF corresponding to a generated JSON.

    source_pdf_map is used for single-PDF testing and avoids relying
    on the Reimbursement folder.
    """
    if source_pdf_map:
        direct = source_pdf_map.get(os.path.abspath(json_path))

        if direct and os.path.isfile(direct):
            return direct

    base_name = os.path.splitext(os.path.basename(json_path))[0]

    for sfx in ["_commercial", "_medicare_advantage", "_unknown"]:
        if base_name.endswith(sfx):
            base_name = base_name[: -len(sfx)]
            break

    for input_dir in INPUT_DIRS:
        # Normal batch location
        candidate = os.path.join(
            input_dir,
            base_name + ".pdf",
        )

        if os.path.isfile(candidate):
            return candidate

        # Case-insensitive fallback.
        if os.path.isdir(input_dir):
            target = base_name.casefold()

            for filename in os.listdir(input_dir):
                if not filename.lower().endswith(".pdf"):
                    continue

                if os.path.splitext(filename)[0].casefold() == target:
                    return os.path.join(
                        input_dir,
                        filename,
                    )
    return None


def _get_direct_reimbursement_content(
    section,
    return_tables=False,
):
    """
    Return ONLY direct Reimbursement Guidelines text.

    When the source section contains mixed content, contiguous structured
    row dictionaries are treated as table blocks. They are NOT converted
    into prose. This is what prevents extraction_rules.json from turning:

        {"key": "97810", "value": "1"}

    into:

        "97810: 1"

    The parent text may be cleaned, but table rows are never mutated.
    """
    parent_content = section.get("content", "")

    direct_text_parts = []
    table_blocks = []

    if isinstance(parent_content, list):
        current_table = []

        def flush_table():
            nonlocal current_table
            if current_table:
                table_blocks.append(
                    {
                        "type": "table",
                        "content": current_table,
                    }
                )
                current_table = []

        for item in parent_content:
            if isinstance(item, dict):
                # Mixed-section table rows use key/value pairs.
                if "key" in item and "value" in item:
                    current_table.append(
                        {
                            "key": item.get("key"),
                            "value": item.get("value"),
                        }
                    )
                    continue

            flush_table()

            if isinstance(item, str) and item.strip():
                direct_text_parts.append(item.strip())

        flush_table()

        cleaned = "\n".join(direct_text_parts).strip()

    elif isinstance(parent_content, str):
        cleaned = parent_content.strip()

    else:
        cleaned = str(parent_content or "").strip()

    # If direct content is just a TOC string or history note, clear it
    toc_keywords = {
        "definitions",
        "definition",
        "questions and answers",
        "question and answers",
        "codes",
        "applicable codes",
        "procedure codes",
        "attachments",
        "resources",
        "history",
        "history section",
        "application",
        "policy",
    }
    words = [w.lower() for w in cleaned.split()]
    if words and len(words) <= 15 and all(w in toc_keywords for w in words):
        cleaned = ""
    elif re.match(
        r"^\d{1,2}/\d{1,2}/\d{2,4}\s+(?:new policy|policy published|policy version change)",
        cleaned,
        re.I,
    ):
        cleaned = ""

    # Remove subsection text ONLY from the parent's direct text.
    if isinstance(cleaned, str) and cleaned:

        def remove_nested(items):
            nonlocal cleaned

            if not isinstance(items, list):
                return

            for item in items:
                if not isinstance(item, dict):
                    continue

                sub_content = item.get("content", "")

                if isinstance(sub_content, list):
                    sub_content = " ".join(
                        part.strip()
                        for part in sub_content
                        if isinstance(part, str) and part.strip()
                    )

                if isinstance(sub_content, str) and sub_content.strip():
                    cleaned = _remove_subsection_content_from_parent(
                        cleaned,
                        sub_content,
                    )

                remove_nested(item.get("subsections", []))
                remove_nested(item.get("children", []))

        remove_nested(section.get("subsections", []))
        remove_nested(section.get("children", []))

        cleaned = re.sub(
            r"\n{3,}",
            "\n\n",
            cleaned,
        ).strip()

    cleaned = normalize_unicode_punctuation(cleaned)

    if return_tables:
        return cleaned, table_blocks

    return cleaned


def _find_reimbursement_guidelines_section(
    sections,
):
    """Find the actual Reimbursement Guidelines section recursively."""
    if not isinstance(sections, list):
        return None

    def has_substance(sec):
        if not sec or not isinstance(sec, dict):
            return False
        content = sec.get("content", "")
        if isinstance(content, list) and content:
            return True
        if isinstance(content, str) and len(content.strip()) > 30:
            return True
        if sec.get("subsections") or sec.get("tables") or sec.get("children"):
            return True
        return False

    # Priority 1: explicitly titled Reimbursement Guidelines or Reimbursement that has substance
    for section in sections:
        if not isinstance(section, dict):
            continue

        title = normalize_heading_text(section.get("title", ""))

        if title in (
            "reimbursement guidelines",
            "reimbursement guideline",
            "reimbursement",
            "reimbursement methodology",
        ) or ("reimbursement" in title and "guideline" in title):
            if has_substance(section):
                return section

            found = _find_reimbursement_guidelines_section(section.get("children", []))
            if found and has_substance(found):
                return found

        found = _find_reimbursement_guidelines_section(section.get("children", []))

        if found and has_substance(found):
            return found

    # Priority 2: Section titled "Policy", "Overview", or "Coverage Criteria" that has substance or valid children
    for section in sections:
        if not isinstance(section, dict):
            continue

        title = normalize_heading_text(section.get("title", ""))

        if title in ("policy", "overview", "coverage criteria"):
            # Check children for any non-overview child
            for ch in section.get("children", []):
                ch_title = normalize_heading_text(ch.get("title", ""))
                if ch_title != "overview" and has_substance(ch):
                    return ch
            found = _find_reimbursement_guidelines_section(section.get("children", []))
            if found and has_substance(found):
                return found

            if has_substance(section):
                return section

    # Priority 3: Any section titled "overview" or "coverage criteria"
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = normalize_heading_text(section.get("title", ""))
        if title in ("overview", "coverage criteria") and has_substance(section):
            return section

    # Final fallback: return any section with reimbursement, policy, or overview
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = normalize_heading_text(section.get("title", ""))
        if "reimbursement" in title or title in ("policy", "overview"):
            return section

    return None


def generate_extracted_rules(
    json_paths=None,
    source_pdf_map=None,
):
    """
    Generate extraction_rules.json.

    IMPORTANT Reimbursement Guidelines behavior:
      - Parent "content" contains ONLY direct RG content.
      - Content belonging to RG subsections is removed ONLY from
        the parent's content.
      - Every subsection is preserved separately as:
            {
                "title": "...",
                "content": "..."
            }
      - Subsection content itself is NEVER modified.
    """
    if not os.path.isdir(OUTPUT_DIR):
        return

    if json_paths is None:
        json_files = sorted(f for f in os.listdir(OUTPUT_DIR) if f.endswith(".json"))

        json_paths = [
            os.path.join(
                OUTPUT_DIR,
                filename,
            )
            for filename in json_files
        ]
    else:
        json_paths = [
            os.path.abspath(path) for path in json_paths if os.path.isfile(path)
        ]

    extracted_rules = []

    # --------------------------------------------------------
    # REIMBURSEMENT GUIDELINES SUBSECTION BUILDER
    # --------------------------------------------------------
    def build_rg_subsections(
        subsections,
        children=None,
    ):
        """
        Export semantic RG subsections without flattening table data.

        Generic no-TOC parsing can place headings such as "Definitions"
        in rg_section["children"], while the specialized RG parser places
        headings found in direct RG text in rg_section["subsections"].
        Support both paths.
        """
        result = []

        def add_item(
            title,
            content=None,
            nested_subsections=None,
        ):
            item = {"title": title or ""}

            if isinstance(content, list):
                text_parts = []
                tables = []
                current_table = []

                def flush_table():
                    nonlocal current_table
                    if current_table:
                        tables.append(
                            {
                                "type": "table",
                                "content": current_table,
                            }
                        )
                        current_table = []

                for value in content:
                    if isinstance(value, dict) and "key" in value and "value" in value:
                        current_table.append(
                            {
                                "key": value.get("key"),
                                "value": value.get("value"),
                            }
                        )
                    else:
                        flush_table()
                        if isinstance(value, str) and value.strip():
                            text_parts.append(value.strip())

                flush_table()

                if text_parts:
                    item["content"] = "\n".join(text_parts)

                if tables:
                    item["tables"] = tables

            elif content is not None:
                item["content"] = content

            if nested_subsections:
                item["subsections"] = build_rg_subsections(nested_subsections)

            result.append(item)

        if isinstance(subsections, list):
            for subsection in subsections:
                if not isinstance(subsection, dict):
                    continue

                add_item(
                    subsection.get("title", ""),
                    subsection.get("content"),
                    subsection.get("subsections"),
                )

        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue

                add_item(
                    child.get("title", ""),
                    child.get("content"),
                    child.get("children"),
                )

        return result

    for json_path in json_paths:
        with open(
            json_path,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        dates = data.get(
            "policy_dates",
            {},
        )

        sections = data.get(
            "sections",
            [],
        )

        rule_add = []

        # --------------------------------------------------------
        # QUESTION / ANSWER RULES
        #
        # Structured per-row Q&A pairs (produced by parse_qa_grid_rows
        # from the actual PyMuPDF table grid) are used directly here.
        # Only genuinely unstructured leftover text falls back to the
        # QUESTION_ANSWER_BLOCK_REGEX text-block scan.
        # --------------------------------------------------------
        def find_all_qa_sections(sec_list):
            res = []
            for s in sec_list:
                if not isinstance(s, dict):
                    continue
                s_title = normalize_heading_text(s.get("title", ""))
                if s_title in (
                    "questions and answers",
                    "question and answers",
                    "questions & answers",
                    "question & answers",
                    "q&a",
                    "q & a",
                ):
                    res.append(s)
                res.extend(find_all_qa_sections(s.get("children", [])))
            return res

        for section in find_all_qa_sections(sections):
            rules = {}
            rule_num = 1
            leftover_text_parts = []

            def add_rule_if_relevant(q_text, a_text):
                nonlocal rule_num

                if not q_text or not a_text:
                    return

                q_clean = normalize_unicode_punctuation(str(q_text)).strip()
                a_clean = normalize_unicode_punctuation(str(a_text)).strip()

                # Clean leading 'Q:', 'Q1:', '1.', etc. from question
                q_clean = re.sub(
                    r"^(?:Q\d*[:\.\s\-]+|\d+[\.\:\s\-]+)", "", q_clean
                ).strip()
                # Clean leading 'A:', 'A1:', etc. from answer
                a_clean = re.sub(r"^(?:A\d*[:\.\s\-]+)", "", a_clean).strip()

                if len(q_clean) < 3 or len(a_clean) < 2:
                    return

                for existing in rules.values():
                    if existing.get("Q") == q_clean:
                        return

                rules[f"Rule {rule_num}"] = {
                    "Q": q_clean,
                    "A": a_clean,
                }
                rule_num += 1

            section_content = section.get("content")

            if section.get("type") in ("table", "mixed") and isinstance(
                section_content, list
            ):
                for item in section_content:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("value"), dict)
                        and "Q" in item["value"]
                        and "A" in item["value"]
                    ):
                        # Direct per-row Q&A pair from the table grid.
                        add_rule_if_relevant(
                            item["value"].get("Q", ""),
                            item["value"].get("A", ""),
                        )

                    elif isinstance(item, dict):
                        key = item.get("key", "")
                        value_text = str(item.get("value") or "")

                        leftover_text_parts.append(f"{key} {value_text}".strip())

                    elif isinstance(item, str):
                        leftover_text_parts.append(item)

            else:
                leftover_text_parts.append(str(section_content or ""))

            if leftover_text_parts:
                content = "\n".join(leftover_text_parts)

                qa_blocks = QUESTION_ANSWER_BLOCK_REGEX.findall(
                    content,
                )

                for qa in qa_blocks:
                    idx = qa.find("A:")

                    if idx < 0:
                        continue

                    add_rule_if_relevant(
                        qa[:idx][3:],
                        qa[idx + 2 :],
                    )

            if rules:
                rule_add.append(rules)

        # --------------------------------------------------------
        # REIMBURSEMENT GUIDELINES
        # --------------------------------------------------------
        rg_section = _find_reimbursement_guidelines_section(sections)

        if rg_section is not None:
            # IMPORTANT:
            # _get_direct_reimbursement_content() edits ONLY the
            # parent's content. It does NOT mutate rg_section or
            # any subsection content.
            direct_rg_content, rg_tables = _get_direct_reimbursement_content(
                rg_section,
                return_tables=True,
            )

            rg_title = rg_section.get(
                "title",
                "Reimbursement Guidelines",
            )
            if normalize_heading_text(rg_title) in ("policy", "overview"):
                rg_title = "Reimbursement Guidelines"

            rg_payload = {
                "title": rg_title,
                "content": direct_rg_content,
            }

            # Preserve reimbursement tables as structured objects.
            # Do NOT stringify their rows into the prose content.
            if rg_tables:
                rg_payload["tables"] = rg_tables

            # Preserve the subsections separately.
            # Their original content is copied as-is.
            policy_section = None
            for sec in sections:
                if normalize_heading_text(sec.get("title", "")) == "policy":
                    policy_section = sec
                    break

            extra_children = []
            if policy_section and isinstance(policy_section.get("children"), list):
                found_rg = False
                for ch in policy_section["children"]:
                    ch_title = normalize_heading_text(ch.get("title", ""))
                    if ch_title == "overview":
                        continue
                    if "reimbursement" in ch_title and "guideline" in ch_title:
                        found_rg = True
                        continue
                    if found_rg:
                        extra_children.append(ch)

            if (
                not extra_children
                and not rg_section.get("subsections")
                and not rg_section.get("children")
            ):
                terminal_secs = {
                    "definitions",
                    "definition",
                    "questions and answers",
                    "question and answers",
                    "resources",
                    "history",
                    "attachments",
                    "codes",
                }
                rg_idx = None
                for i, sec in enumerate(sections):
                    if sec is rg_section or sec.get("title") == rg_section.get("title"):
                        rg_idx = i
                        break
                if rg_idx is not None:
                    for sec in sections[rg_idx + 1 :]:
                        if (
                            normalize_heading_text(sec.get("title", ""))
                            in terminal_secs
                        ):
                            break
                        extra_children.append(sec)

            rg_subsections = build_rg_subsections(
                rg_section.get("subsections", []),
                children=(rg_section.get("children", []) or []) + extra_children,
            )

            if rg_subsections:
                rg_payload["subsections"] = rg_subsections

        else:
            rg_payload = {
                "title": "Reimbursement Guidelines",
                "content": "",
            }

        # --------------------------------------------------------
        # LINKED PDF CPT CODES
        # --------------------------------------------------------
        linked_pdf_contents = data.get("linked_pdf_contents", [])

        cpt_codes_used = list(data.get("CPT_CODES_USED", []))

        for linked_doc_entry in linked_pdf_contents or []:
            for code in linked_doc_entry.get("CPT_CODES_USED", []):
                if code not in cpt_codes_used:
                    cpt_codes_used.append(code)

        # --------------------------------------------------------
        # RESOURCES
        # Find the "Resources" section inside the per-file sections
        # list and extract its content. data.get("Resources") is always
        # None because "Resources" is not a top-level key in the
        # per-file JSON — it lives as a node inside "sections".
        # --------------------------------------------------------
        resources_content = None
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if normalize_heading_text(sec.get("title", "")) == "resources":
                resources_content = sec.get("content") or None
                break

        policy_rule = {
            "policy_title": data.get("policy_title"),
            "policy_number": data.get("policy_number"),
            "effective_date": dates.get("effective_date"),
            "expiry_date": dates.get("expiry_date"),
            "Line_of_Business": data.get("Line_of_Business"),
            "Billingclass": data.get("Billingclass"),
            "CPT_CODES_USED": cpt_codes_used,
            "reimbursement_guidelines": rg_payload,
            "Question_Answer_Rules": (rule_add or None),
            "linked_pdf_contents": (linked_pdf_contents or None),
            "resources": resources_content,
        }

        extracted_rules.append(policy_rule)

    with open(
        OUTPUT_RULES_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            extracted_rules,
            f,
            indent=2,
            ensure_ascii=False,
        )

    return OUTPUT_RULES_FILE


def export_policy_titles_csv():
    if not os.path.isdir(OUTPUT_DIR):
        return None

    json_files = sorted(f for f in os.listdir(OUTPUT_DIR) if f.endswith(".json"))

    titles = []

    for jfname in json_files:
        with open(os.path.join(OUTPUT_DIR, jfname), "r", encoding="utf-8") as f:
            data = json.load(f)

        title = data.get("policy_title")

        if title:
            titles.append(title)

    with open(OUTPUT_TITLES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["policy_title"])

        for title in titles:
            writer.writerow([title])

    return OUTPUT_TITLES_CSV


# ============================================================
# MAIN - BATCH EXTRACTION
# ============================================================


def main():
    try:
        sys.stdout.reconfigure(
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        pass

    # --------------------------------------------------------
    # 1. Validate input directory
    # --------------------------------------------------------

    existing_input_dirs = [
        input_dir for input_dir in INPUT_DIRS if os.path.isdir(input_dir)
    ]

    if not existing_input_dirs:
        print("\nERROR: Neither input directory was found:")
        for input_dir in INPUT_DIRS:
            print(input_dir)
        return

    # --------------------------------------------------------
    # 2. Create output directory
    # --------------------------------------------------------

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --------------------------------------------------------
    # 3. Find ALL PDFs in Reimbursement/
    # --------------------------------------------------------

    pdf_files = find_pdf_files(existing_input_dirs)

    if not pdf_files:
        return

    # --------------------------------------------------------
    # 4. Extract every PDF
    # --------------------------------------------------------

    successful = []
    failed = []
    pbar = tqdm(
        total=len(pdf_files), position=0, leave=True, miniters=1, colour="#C788C7"
    )
    for index, pdf_path in enumerate(pdf_files, start=1):
        pbar.update(1)

        try:
            json_path = process_pdf(pdf_path)

            if json_path and os.path.isfile(json_path):
                successful.append(
                    {
                        "pdf": pdf_path,
                        "json": json_path,
                    }
                )
            else:
                failed.append(
                    {
                        "pdf": pdf_path,
                        "error": "JSON was not created",
                    }
                )

        except Exception as exc:

            failed.append(
                {
                    "pdf": pdf_path,
                    "error": str(exc),
                }
            )
    pbar.close()

    # --------------------------------------------------------
    # 5. Generate extraction_rules.json
    #
    # IMPORTANT:
    # This runs only AFTER all PDFs are extracted.
    # With json_paths=None, generate_extracted_rules()
    # reads all JSON files currently present in OUTPUT_DIR.
    # --------------------------------------------------------

    try:
        rules_path = generate_extracted_rules()

        if rules_path:
            print(f"\nExtraction rules created:\n{rules_path}")

    except Exception as exc:
        print("\nERROR generating extraction rules:")

    # --------------------------------------------------------
    # 7. Final summary
    # --------------------------------------------------------

    print(f"\nTotal PDFs found : {len(pdf_files)}")
    print(f"Successful       : {len(successful)}")
    print(f"Failed           : {len(failed)}")

    if failed:

        for item in failed:
            print(f"\n{os.path.basename(item['pdf'])}")


if __name__ == "__main__":
    main()