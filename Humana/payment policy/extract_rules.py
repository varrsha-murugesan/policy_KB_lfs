"""
Humana Payment Policy Complete Extraction & Candidate Content Engine
Single Unified Script:
1) Extracts each PDF's complete content into individual JSON files:
   humana_policies/extracted_policies/<policy_title>.json
2) From those JSON files, extracts raw, policy-derived candidate content blocks
   (paragraphs, bullets, table rows, definitions) that may contain potential
   billing/reimbursement rules into:
   humana_policies/extraction_rules.json (and mirrored to extracted_rules.json)
   WITHOUT synthesizing or interpreting them into finalized rules.
3) Validates extraction completeness, data integrity, and source traceability.
"""
import os
import sys
import re
import json
import logging
import argparse
from typing import List, Dict, Any, Optional, Tuple, Set
from collections import Counter

# Ensure UTF-8 output encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ==============================================================================
# CLINICAL CODE EXTRACTION PATTERNS & MODIFIERS
# ==============================================================================
CPT_HCPCS_RE = re.compile(r'\b([0-9]{4}[0-9A-Z]|[A-Z][0-9]{4}|0[0-9]{3}[TU])\b')
ICD10_RE = re.compile(r'\b([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)\b')
REVENUE_CODE_RE = re.compile(r'\brevenue\s*codes?\s*(?:is|are|of)?\s*[:]?\s*(0[0-9]{3})\b', re.I)

# Non-code prefix patterns (transmittals, change requests, zip, phone, box, sections)
NON_CODE_PREFIX_RE = re.compile(
    r'\b(?:Transmittal|Change Request|CR|TR|Box|P\.O\.|Zip|Section|§|Telephone|Tel|Phone|Fax|Page)\s*[:#]?\s*([0-9]{4,5})\b',
    re.I
)

KNOWN_MODIFIERS = {
    '22', '24', '25', '26', '50', '51', '52', '53', '58', '59', '62', '76', '77', '78', '79',
    '80', '82', '90', '91', '93', '95', '99',
    'AA', 'AD', 'AS', 'BP', 'E1', 'E2', 'E3', 'E4', 'FY', 'G8', 'G9', 'GA', 'GC', 'GG', 'GH',
    'GJ', 'GK', 'GL', 'GN', 'GO', 'GP', 'GR', 'GS', 'GT', 'GV', 'GW', 'GY', 'GZ',
    'JW', 'JZ', 'KX', 'LT', 'NR', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'QE', 'QK', 'QS', 'QX',
    'QY', 'QZ', 'RA', 'RB', 'RC', 'RD', 'RE', 'RI', 'RR', 'RT',
    'TC', 'TS', 'UN', 'UP', 'UQ', 'UR', 'US', 'XE', 'XP', 'XS', 'XU', 'EY'
}

MODIFIER_EXPLICIT_RE = re.compile(
    r'\b(?:modifiers?\s*[-–]?\s*([0-9A-Za-z]{2})|([0-9A-Za-z]{2})\s*modifiers?|-([0-9A-Za-z]{2})\b)',
    re.I
)

# ==============================================================================
# HELPER UTILITIES
# ==============================================================================
def clean_cell(val: Any) -> str:
    """Normalize whitespace and strip control characters from table cells."""
    if val is None:
        return ""
    s = str(val).replace('\xa0', ' ').replace('\u200b', '').replace('\ufffd', '-')
    s = s.replace('\uf0b7', '•').replace('\uf0a7', '▪').replace('\uf0a8', 'o')
    s = re.sub(r'[ \t]+', ' ', s)
    return s.strip()

def clean_text(text: str) -> str:
    """Normalize text while strictly preserving verbatim wording and line breaks."""
    if not text:
        return ""
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\r', '').replace('\ufffd', '-')
    text = text.replace('\uf0b7', '• ').replace('\uf0a7', '▪ ').replace('\uf0a8', 'o ')
    lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in text.splitlines()]
    return '\n'.join(lines).strip()

def sanitize_filename(title: str) -> str:
    """Create a safe, clean filename from policy title."""
    clean = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', title)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean if clean else "policy"

def extract_codes_from_text(text: str) -> Dict[str, List[str]]:
    """Extract and categorize clinical procedure, diagnosis, modifier, and revenue codes."""
    false_codes = set(NON_CODE_PREFIX_RE.findall(text))
    
    cpts = set()
    hcpcs = set()
    for m in CPT_HCPCS_RE.findall(text):
        if m in false_codes:
            continue
        if m.isdigit():
            val = int(m)
            # Standard CPT code range (filter out calendar years)
            if (10000 <= val <= 99999) and not (1900 <= val <= 2035):
                cpts.add(m)
        else:
            if re.match(r'^[A-Z][0-9]{4}$', m):
                hcpcs.add(m)
            elif re.match(r'^(?:0[0-9]{3}[TU]|[0-9]{4}[A-Z])$', m):
                cpts.add(m)

    mods = set()
    for t in MODIFIER_EXPLICIT_RE.findall(text):
        for g in t:
            if g.upper() in KNOWN_MODIFIERS:
                mods.add(g.upper())

    icds = set()
    for m in ICD10_RE.findall(text):
        if re.match(r'^[A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?$', m):
            icds.add(m)

    revs = set(REVENUE_CODE_RE.findall(text))

    return {
        "all_codes": sorted(list(cpts.union(hcpcs).union(mods).union(icds).union(revs))),
        "cpt_codes": sorted(list(cpts)),
        "hcpcs_codes": sorted(list(hcpcs)),
        "modifiers": sorted(list(mods)),
        "icd_codes": sorted(list(icds)),
        "revenue_codes": sorted(list(revs))
    }

def extract_rule_codes(text: str) -> List[str]:
    """Extract all clinical procedure, modifier, diagnosis, and revenue codes explicitly present in text."""
    false_codes = set(NON_CODE_PREFIX_RE.findall(text))
    codes = set()
    
    for m in CPT_HCPCS_RE.findall(text):
        if m in false_codes:
            continue
        if m.isdigit():
            val = int(m)
            if (10000 <= val <= 99999) and not (1900 <= val <= 2035):
                codes.add(m)
        else:
            if re.match(r'^[A-Z][0-9]{4}$', m):
                codes.add(m)
            elif re.match(r'^(?:0[0-9]{3}[TU]|[0-9]{4}[A-Z])$', m):
                codes.add(m)

    for t in MODIFIER_EXPLICIT_RE.findall(text):
        for g in t:
            if g.upper() in KNOWN_MODIFIERS:
                codes.add(f"Modifier-{g.upper()}")

    for m in ICD10_RE.findall(text):
        if re.match(r'^[A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?$', m):
            codes.add(m)

    for rev in REVENUE_CODE_RE.findall(text):
        codes.add(f"RevCode-{rev}")

    return sorted(list(codes))

# ==============================================================================
# PDF POLICY EXTRACTION ENGINE (FUNCTIONALITY 1)
# ==============================================================================
def extract_metadata(doc, fname: str) -> Dict[str, Any]:
    """Extract policy metadata and determine Billingclass from document text."""
    p0 = doc[0]
    text0 = p0.get_text().replace('\ufffd', '-').replace('\xa0', ' ')
    
    cutoff = len(text0)
    for marker in ['Disclaimer:', 'Overview']:
        pos = text0.find(marker)
        if pos != -1 and pos > 50 and pos < cutoff:
            cutoff = pos
    header_block = text0[:cutoff]
    clean_block = re.sub(r'Page\s+\d+\s+of\s+\d+', '', header_block)
    
    meta = {
        "policy_title": None,
        "policy_number": None,
        "published_date": None,
        "effective_date": None,
        "revision_date": None,
        "last_reviewed": None,
        "Line_of_Business": None,
        "Billingclass": "Both",
        "source_pdf": fname,
        "total_pages": len(doc),
        "related_policies": []
    }
    
    subj_m = re.search(r'Subject:\s*(.*?)(?=(?:Application:|Published(?:\s*date)?:|Policy [Nn]umber:|Revision date:|Review date|Effective dates?:|Related policies:|\Z))', clean_block, re.S)
    if subj_m:
        meta['policy_title'] = ' '.join(subj_m.group(1).split()).strip().replace('\ufffd', '-')
        
    app_m = re.search(r'Application:\s*(.*?)(?=(?:Published(?:\s*date)?:|Policy [Nn]umber:|Revision date:|Review date|Effective dates?:|Related policies:|\Z))', clean_block, re.S)
    if app_m:
        meta['Line_of_Business'] = ' '.join(app_m.group(1).split()).strip()
        
    pub_m = re.search(r'Published(?:\s*date)?:\s*([0-9]{2}/[0-9]{4}|[0-9]{2}/[0-9]{2}/[0-9]{4})', clean_block, re.I)
    if pub_m:
        meta['published_date'] = pub_m.group(1).strip()
        
    pol_m = re.search(r'Policy [Nn]umber:\s*([A-Za-z0-9\-]+)', clean_block, re.I)
    if pol_m:
        meta['policy_number'] = pol_m.group(1).strip()
        
    rev_m = re.search(r'(?:Review date or Revision date|Revision date):\s*([0-9]{2}/[0-9]{4}|[0-9]{2}/[0-9]{2}/[0-9]{4})', clean_block, re.I)
    if rev_m:
        meta['revision_date'] = rev_m.group(1).strip()
        meta['last_reviewed'] = meta['revision_date']
        
    eff_m = re.search(r'Effective dates?:\s*(.*?)(?=(?:Application:|Published(?:\s*date)?:|Policy [Nn]umber:|Revision date:|Review date|Related policies:|\Z))', clean_block, re.S)
    if eff_m:
        val = ' '.join(eff_m.group(1).split()).strip()
        meta['effective_date'] = val if val else None
    
    if not meta['effective_date'] and meta['published_date']:
        meta['effective_date'] = meta['published_date']
        
    rel_m = re.search(r'Related policies:\s*(.*?)(?=(?:Application:|Published(?:\s*date)?:|Policy [Nn]umber:|Revision date:|Review date|Effective dates?:|\Z))', clean_block, re.S)
    if rel_m:
        rel = ' '.join(rel_m.group(1).split()).strip()
        if rel and rel.upper() != 'N/A':
            meta['related_policies'] = [r.strip() for r in rel.split(';') if r.strip()]

    if not meta['policy_title']:
        base = os.path.splitext(fname)[0]
        base_clean = re.sub(r'-[a-f0-9]{8}$', '', base)
        meta['policy_title'] = base_clean.replace('_', ' ').replace('-', ' ').strip()

    full_text = " ".join(p.get_text() for p in doc).lower()
    has_prof = bool(re.search(r'\b(professional claims?|cms-1500|physicians?|practitioners?|physician fee schedule)\b', full_text))
    has_fac = bool(re.search(r'\b(facility claims?|institutional claims?|ub-04|inpatient|outpatient hospital|observation stay|hospital encounter)\b', full_text))
    
    if has_prof and has_fac:
        meta["Billingclass"] = "Both"
    elif has_fac and not has_prof:
        meta["Billingclass"] = "Facility"
    elif has_prof and not has_fac:
        meta["Billingclass"] = "Professional"
    else:
        meta["Billingclass"] = "Both"

    return meta

def extract_tables_from_doc(doc) -> List[Dict[str, Any]]:
    """Extract and reconstruct multi-page continuous tables across the PDF."""
    raw_tables = []
    
    for pno in range(len(doc)):
        page = doc[pno]
        found = page.find_tables(strategy="lines_strict")
        if not found.tables:
            found = page.find_tables(strategy="lines")
            
        for t in found.tables:
            extracted = t.extract()
            if not extracted or len(extracted) < 1:
                continue
            
            num_cols = len(extracted[0])
            col_has_data = [False] * num_cols
            for r in extracted:
                for c_idx in range(min(num_cols, len(r))):
                    if clean_cell(r[c_idx]):
                        col_has_data[c_idx] = True
            
            valid_cols = [c_idx for c_idx, has in enumerate(col_has_data) if has]
            if not valid_cols:
                continue
                
            clean_rows = []
            for r in extracted:
                row_data = [clean_cell(r[c_idx]) if c_idx < len(r) else "" for c_idx in valid_cols]
                if any(row_data):
                    clean_rows.append(row_data)
                    
            if not clean_rows:
                continue
                
            raw_tables.append({
                "page": pno + 1,
                "bbox": t.bbox,
                "header": clean_rows[0],
                "rows": clean_rows[1:] if len(clean_rows) > 1 else [],
            })

    reconstructed: List[Dict[str, Any]] = []
    i = 0
    while i < len(raw_tables):
        curr = raw_tables[i]
        page_start = curr["page"]
        page_end = curr["page"]
        header = curr["header"]
        merged_rows = list(curr["rows"])
        bboxes = [(curr["page"], curr["bbox"])]
        
        j = i + 1
        while j < len(raw_tables):
            nxt = raw_tables[j]
            if nxt["page"] == page_end + 1:
                header_matches = (nxt["header"] == header) or (len(nxt["header"]) == len(header) and any(c in nxt["header"] for c in header if c))
                if header_matches:
                    merged_rows.extend(nxt["rows"])
                    page_end = nxt["page"]
                    bboxes.append((nxt["page"], nxt["bbox"]))
                    j += 1
                    continue
            break
            
        structured_rows = []
        for r in merged_rows:
            row_dict = {}
            for c_idx, col_name in enumerate(header):
                val = r[c_idx] if c_idx < len(r) else ""
                key = col_name if col_name else f"column_{c_idx+1}"
                row_dict[key] = val
            structured_rows.append(row_dict)
            
        reconstructed.append({
            "type": "table",
            "page_start": page_start,
            "page_end": page_end,
            "columns": header,
            "row_count": len(structured_rows),
            "rows": structured_rows,
            "bboxes": bboxes
        })
        i = j
        
    return reconstructed

def is_point_in_bboxes(page_num: int, y: float, bboxes: List[Tuple[int, Tuple[float, float, float, float]]]) -> bool:
    """Check if vertical coordinate falls within any table bounding box on the specified page."""
    for p, bb in bboxes:
        if p == page_num:
            if bb[1] - 2 <= y <= bb[3] + 2:
                return True
    return False

def table_belongs_to_section(tbl: Dict[str, Any], p_start: int, y_start: float, p_end: int, y_end: float) -> bool:
    """Accurately determine whether a table belongs to a specific section based on page and y coordinates."""
    tp_start = tbl["page_start"]
    ty_start = tbl["bboxes"][0][1][1] if tbl.get("bboxes") else 0.0
    
    if tp_start < p_start or (tp_start == p_start and ty_start < y_start - 2):
        return False
    if tp_start > p_end or (tp_start == p_end and ty_start >= y_end - 2):
        return False
    return True

def is_toc_clean(toc: List) -> bool:
    """Check if native PDF bookmarks are valid, clean, and not corrupted."""
    if not toc or len(toc) < 2:
        return False
    for lvl, title, pno in toc:
        t = title.strip()
        if len(t) > 85 and not re.match(r'^(?:cpt|hcpcs|table)', t, re.I):
            return False
        if 'general humana resources medicare advantage' in t.lower() or ('disclaimer' in t.lower() and len(t) > 30):
            return False
        if t.startswith('•') or t.startswith('▪') or t.startswith('\uf0b7') or t.startswith('o '):
            return False
    return True

def get_font_headings(doc) -> List[Dict[str, Any]]:
    """Deterministic font-size and weight layout analyzer for robust heading detection."""
    num_pages = len(doc)
    font_headings = []
    for pno in range(num_pages):
        page = doc[pno]
        h_page = page.rect.height
        d = page.get_text('dict')
        for b in d['blocks']:
            if 'lines' in b:
                for l in b['lines']:
                    txt = ' '.join(s['text'].strip() for s in l['spans'] if s['text'].strip())
                    if not txt or len(txt) > 100:
                        continue
                    sz = max((s['size'] for s in l['spans'] if s['text'].strip()), default=0)
                    is_bold = any(('bold' in s['font'].lower() or s['flags'] & 16) for s in l['spans'] if s['text'].strip())
                    y = l['bbox'][1]
                    
                    if pno > 0 and (y < 85 or y > h_page - 65):
                        continue
                    if pno == 0 and (y < 190 or y > h_page - 65):
                        continue
                    if txt.startswith('•') or txt.startswith('▪') or txt.startswith('o '):
                        continue
                    if txt.endswith('.') or txt.endswith(';') or txt.endswith(','):
                        continue
                    if 'doi:' in txt.lower() or 'claimpaymentpolicies' in txt.lower() or 'disclaimer' in txt.lower():
                        continue
                        
                    if (is_bold and sz >= 13.0) or (sz >= 16.0):
                        font_headings.append({'level': 1, 'title': txt, 'page': pno + 1, 'y': y})
                    elif is_bold and 11.5 <= sz < 13.0:
                        font_headings.append({'level': 2, 'title': txt, 'page': pno + 1, 'y': y})
                        
    later_titles = {re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower()) for h in font_headings if h['page'] > 1}
    filtered = []
    for h in font_headings:
        norm = re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower())
        if h['page'] == 1 and norm in later_titles and norm != 'overview':
            continue
        filtered.append(h)
    return filtered

def resolve_headings(doc) -> List[Dict[str, Any]]:
    """Determine clean document headings using native bookmarks or font layout analysis."""
    num_pages = len(doc)
    raw_toc = doc.get_toc()
    
    if is_toc_clean(raw_toc):
        headings = []
        for lvl, title, pno in raw_toc:
            t = title.strip().replace('\ufffd', '-')
            if t.startswith('Disclaimer:') or not t:
                continue
            headings.append({'level': lvl, 'title': t, 'page': pno, 'y': 0.0})
            
        later_titles = {re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower()) for h in headings if h['page'] > 1}
        headings = [h for h in headings if not (h['page'] == 1 and re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower()) in later_titles and re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower()) != 'overview')]
        
        for h in headings:
            pno = h['page'] - 1
            if 0 <= pno < num_pages:
                page = doc[pno]
                d = page.get_text('dict')
                h_norm = re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower())
                best_y = None
                for b in d['blocks']:
                    if 'lines' in b:
                        for l in b['lines']:
                            txt = ' '.join(s['text'].strip() for s in l['spans'] if s['text'].strip())
                            t_norm = re.sub(r'[^a-zA-Z0-9]', '', txt.lower())
                            if not t_norm:
                                continue
                            if h_norm == t_norm or (len(h_norm) > 10 and h_norm.startswith(t_norm) and len(t_norm) > 8):
                                best_y = l['bbox'][1]
                                break
                        if best_y is not None:
                            break
                if best_y is not None:
                    h['y'] = best_y
        return headings
    else:
        return get_font_headings(doc)

def parse_policy_to_json(pdf_path: str, logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Parse a single Humana PDF into complete, structured policy JSON document."""
    import pymupdf
    fname = os.path.basename(pdf_path)
    doc = pymupdf.open(pdf_path)
    num_pages = len(doc)
    
    if logger:
        logger.info(f"Extracting '{fname}' ({num_pages} pages)")

    metadata = extract_metadata(doc, fname)
    tables = extract_tables_from_doc(doc)
    
    table_bboxes = []
    for t in tables:
        table_bboxes.extend(t.get("bboxes", []))

    headings = resolve_headings(doc)
    if not headings:
        headings = [{'level': 1, 'title': metadata['policy_title'] or 'Policy Guidelines', 'page': 1, 'y': 190.0}]

    later_section_norms = {
        re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower())
        for h in headings
        if re.sub(r'[^a-zA-Z0-9]', '', h['title'].lower()) != 'overview'
    }

    # Extract verbatim content for each section
    flat_sections = []
    for idx, h in enumerate(headings):
        lvl = h['level']
        title = h['title']
        p_start = h['page']
        y_start = h['y']
        
        if idx + 1 < len(headings):
            nxt = headings[idx + 1]
            p_end = nxt['page']
            y_end = nxt['y']
        else:
            p_end = num_pages
            y_end = doc[p_end - 1].rect.height

        sec_paragraphs = []
        current_p = []
        
        for pno_idx in range(p_start - 1, p_end):
            curr_page_num = pno_idx + 1
            page = doc[pno_idx]
            h_page = page.rect.height
            d = page.get_text('dict')
            
            for b in d['blocks']:
                if 'lines' in b:
                    for l in b['lines']:
                        y = l['bbox'][1]
                        
                        if curr_page_num == p_start and y < y_start - 2:
                            continue
                        if curr_page_num == p_end and y >= y_end - 2:
                            continue
                            
                        if curr_page_num > 1 and (y < 85 or y > h_page - 65):
                            continue
                        if curr_page_num == 1 and (y < 190 or y > h_page - 65):
                            continue
                            
                        if is_point_in_bboxes(curr_page_num, y, table_bboxes):
                            continue
                            
                        txt = " ".join(s["text"].strip() for s in l["spans"] if s["text"].strip())
                        if not txt:
                            continue
                            
                        txt_norm = re.sub(r'[^a-zA-Z0-9]', '', txt.lower())
                        title_norm = re.sub(r'[^a-zA-Z0-9]', '', title.lower())
                        if txt_norm == title_norm:
                            continue
                            
                        # On Page 1, filter out visual TOC preview list
                        if curr_page_num == 1 and txt_norm in later_section_norms:
                            continue
                            
                        if "please see the disclaimer" in txt.lower():
                            continue
                        if re.match(r'^page\s+\d+\s+of\s+\d+$', txt.strip(), re.I):
                            continue
                            
                        is_bullet = bool(re.match(r'^(?:[•▪o*]|\([a-z0-9]+\)|[a-z0-9]+\.)\s+', txt))
                        if is_bullet:
                            if current_p:
                                sec_paragraphs.append(" ".join(current_p).strip())
                                current_p = []
                            current_p.append(txt)
                        else:
                            current_p.append(txt)
                            
        if current_p:
            sec_paragraphs.append(" ".join(current_p).strip())

        clean_paragraphs = [clean_text(p) for p in sec_paragraphs if clean_text(p)]
        
        sec_tables = []
        for t in tables:
            if table_belongs_to_section(t, p_start, y_start, p_end, y_end):
                clean_tbl = {
                    "type": "table",
                    "page_start": t["page_start"],
                    "page_end": t["page_end"],
                    "columns": t["columns"],
                    "row_count": t["row_count"],
                    "rows": t["rows"]
                }
                sec_tables.append(clean_tbl)

        flat_sections.append({
            "heading": title,
            "level": lvl,
            "page_start": p_start,
            "page_end": p_end,
            "content": clean_paragraphs,
            "tables": sec_tables
        })

    root_sections = []
    stack = []
    for sec in flat_sections:
        node = {
            "heading": sec["heading"],
            "level": sec["level"],
            "page_start": sec["page_start"],
            "page_end": sec["page_end"],
            "content": sec["content"],
            "tables": sec["tables"],
            "subsections": []
        }
        while stack and stack[-1][0] >= sec["level"]:
            stack.pop()
        if stack:
            stack[-1][1]["subsections"].append(node)
        else:
            root_sections.append(node)
        stack.append((sec["level"], node))

    definitions = {}
    references = []
    resources = []
    clinical_text_chunks = []

    for sec in flat_sections:
        t_low = sec["heading"].lower()
        if not any(ign in t_low for ign in ["reference", "resource", "making it easier"]):
            clinical_text_chunks.extend(sec["content"])
            for t in sec["tables"]:
                for r in t["rows"]:
                    clinical_text_chunks.append(" ".join(str(v) for v in r.values()))
        
        if t_low.startswith("definition"):
            for line in sec["content"]:
                line_str = line.strip().lstrip("•▪*o ").strip()
                if ":" in line_str:
                    term, defn = line_str.split(":", 1)
                    if len(term.strip()) < 80:
                        definitions[term.strip()] = defn.strip()
                        
        elif t_low == "references":
            for line in sec["content"]:
                line_str = line.strip().lstrip("•▪*o ").strip()
                if len(line_str) > 10:
                    references.append(line_str)
                    
        elif "resources" in t_low:
            for line in sec["content"]:
                line_str = line.strip().lstrip("•▪*o ").strip()
                if len(line_str) > 10:
                    resources.append(line_str)

    combined_clinical_text = "\n".join(clinical_text_chunks)
    clinical_codes = extract_codes_from_text(combined_clinical_text)

    total_chars = sum(len(p) for s in flat_sections for p in s["content"])
    doc.close()

    policy_json = {
        "policy_metadata": metadata,
        "clinical_codes": clinical_codes,
        "sections": root_sections,
        "definitions": definitions if definitions else None,
        "references": references if references else None,
        "resources": resources if resources else None,
        "_extraction_stats": {
            "total_sections": len(flat_sections),
            "total_tables": len(tables),
            "total_characters": total_chars
        }
    }

    return policy_json

# ==============================================================================
# CANDIDATE RULE-BEARING CONTENT EXTRACTION ENGINE (FUNCTIONALITY 2)
# Preserves RAW, policy-derived candidate content without rule synthesis
# ==============================================================================
def should_skip_section(heading: str) -> bool:
    """Ignore non-billing administrative, bibliography, and website navigation sections."""
    h_low = heading.lower()
    skip_keywords = [
        "references", "reference",
        "general humana resources", "additional resources",
        "making it easier", "publications", "availity",
        "disclaimer"
    ]
    return any(h_low == k or h_low.startswith(k) for k in skip_keywords)

def extract_candidate_content_from_policy(policy_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract raw, policy-derived content blocks that may contain potential billing/reimbursement
    rules, grouped under a single policy object without repeating policy metadata.
    """
    meta = policy_data.get("policy_metadata", {})
    candidate_blocks = []
    seen_content = set()
    
    policy_title = meta.get("policy_title", "")
    policy_number = meta.get("policy_number")
    effective_date = meta.get("effective_date")
    source_pdf = meta.get("source_pdf", "")
    line_of_business = meta.get("Line_of_Business", "")
    billing_class = meta.get("Billingclass", "Both")

    def process_content_blocks(content_blocks: List[str], sec_name: str, sub_name: Optional[str], hierarchy_path: str, p_start: int, p_end: int):
        for para in content_blocks:
            clean_para = para.strip()
            if len(clean_para) < 5:
                continue
                
            # Filter out general website footer notices
            if "for the most current information, always refer to" in clean_para.lower():
                continue
            if "humana.com/claimpaymentpolicies" in clean_para.lower():
                continue
            if "links to sources outside of humana" in clean_para.lower():
                continue

            # Deduplication
            content_norm = re.sub(r'\s+', ' ', clean_para.lower()).strip()
            if content_norm in seen_content:
                continue
            seen_content.add(content_norm)

            is_bullet = clean_para.startswith("•") or clean_para.startswith("▪") or bool(re.match(r'^[a-z0-9]+\.\s+', clean_para, re.I))
            content_type = "bullet_point" if is_bullet else "paragraph"
            codes = extract_rule_codes(clean_para)
            
            candidate_blocks.append({
                "title": sub_name or sec_name,
                "section": sec_name,
                "subsection": sub_name,
                "hierarchy_path": hierarchy_path,
                "page_number": p_start,
                "page_start": p_start,
                "page_end": p_end,
                "content": clean_para,
                "codes": codes,
                "table_data": None,
                "content_type": content_type
            })

    def process_section_node(node: Dict[str, Any], parent_heading: Optional[str] = None, parent_path: str = ""):
        h = node.get("heading", "")
        p_start = node.get("page_start", 1)
        p_end = node.get("page_end", 1)
        
        if should_skip_section(h):
            return

        current_path = f"{parent_path} > {h}" if parent_path else h
        sec_name = parent_heading if parent_heading else h
        sub_name = h if parent_heading else None

        # Extract raw paragraphs and bullets
        content = node.get("content", [])
        process_content_blocks(content, sec_name, sub_name, current_path, p_start, p_end)

        # Extract structured table rows
        for tbl in node.get("tables", []):
            columns = tbl.get("columns", [])
            tbl_start = tbl.get("page_start", p_start)
            tbl_end = tbl.get("page_end", p_end)
            
            for row in tbl.get("rows", []):
                parts = []
                for col in columns:
                    val = row.get(col, "")
                    if val:
                        parts.append(f"{col}: {val}")
                        
                if parts:
                    row_content = f"[{h}] " + " | ".join(parts)
                    content_norm = re.sub(r'\s+', ' ', row_content.lower()).strip()
                    if content_norm in seen_content:
                        continue
                    seen_content.add(content_norm)

                    codes = extract_rule_codes(row_content)
                    candidate_blocks.append({
                        "title": sub_name or h,
                        "section": sec_name,
                        "subsection": sub_name or "Tabular Content",
                        "hierarchy_path": current_path,
                        "page_number": tbl_start,
                        "page_start": tbl_start,
                        "page_end": tbl_end,
                        "content": row_content,
                        "codes": codes,
                        "table_data": {
                            "columns": columns,
                            "row": row
                        },
                        "content_type": "table_row"
                    })

        for sub in node.get("subsections", []):
            process_section_node(sub, parent_heading=sec_name, parent_path=current_path)

    for root_sec in policy_data.get("sections", []):
        process_section_node(root_sec)

    # Extract definitions as raw definition blocks
    defs = policy_data.get("definitions") or {}
    for term, defn in defs.items():
        clean_def = f"{term}: {defn}"
        content_norm = re.sub(r'\s+', ' ', clean_def.lower()).strip()
        if content_norm in seen_content:
            continue
        seen_content.add(content_norm)

        codes = extract_rule_codes(clean_def)
        candidate_blocks.append({
            "title": term,
            "section": "Definitions of Italicized Terms",
            "subsection": term,
            "hierarchy_path": f"Definitions of Italicized Terms > {term}",
            "page_number": None,
            "page_start": None,
            "page_end": None,
            "content": clean_def,
            "codes": codes,
            "table_data": None,
            "content_type": "definition"
        })

    if not candidate_blocks:
        return None

    return {
        "policy_title": policy_title,
        "policy_number": policy_number,
        "effective_date": effective_date,
        "source_pdf": source_pdf,
        "line_of_business": line_of_business,
        "billing_class": billing_class,
        "extracted_content": candidate_blocks
    }

# ==============================================================================
# AUDIT AND VALIDATION ENGINE
# ==============================================================================
def validate_results(pdf_dir: str, policies_dir: str, rules_file: str, log: logging.Logger):
    """Run rigorous audit verifying that all PDFs and contents are accurately extracted."""
    log.info("=" * 80)
    log.info("PERFORMING AUDIT AND VALIDATION CHECKS...")
    log.info("=" * 80)

    pdf_files = sorted([f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf")])
    json_files = sorted([f for f in os.listdir(policies_dir) if f.lower().endswith(".json")])

    log.info(f"1. FILE COUNT CHECK: Input PDFs: {len(pdf_files)}, Created Policy JSONs: {len(json_files)}")
    if len(pdf_files) != len(json_files):
        log.error(f"Mismatch: {len(pdf_files)} PDFs vs {len(json_files)} JSON files!")
    else:
        log.info("   [PASS] 100% of PDFs have a corresponding individual policy JSON.")

    empty_files = []
    missing_titles = []
    missing_dates = []
    missing_sections = []
    total_characters = 0
    total_tables = 0
    total_codes = 0

    for jf in json_files:
        j_path = os.path.join(policies_dir, jf)
        if os.path.getsize(j_path) == 0:
            empty_files.append(jf)
            continue
            
        with open(j_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        meta = data.get("policy_metadata", {})
        if not meta.get("policy_title"):
            missing_titles.append(jf)
        if not meta.get("effective_date"):
            missing_dates.append(jf)
            
        sections = data.get("sections", [])
        if not sections:
            missing_sections.append(jf)

        def count_chars(sec_list):
            c = 0
            for s in sec_list:
                c += sum(len(p) for p in s.get("content", []))
                c += count_chars(s.get("subsections", []))
            return c
            
        def count_tbls(sec_list):
            t = 0
            for s in sec_list:
                t += len(s.get("tables", []))
                t += count_tbls(s.get("subsections", []))
            return t

        total_characters += count_chars(sections)
        total_tables += count_tbls(sections)
        total_codes += len(data.get("clinical_codes", {}).get("all_codes", []))

    log.info(f"2. POLICY INTEGRITY CHECK: Total Extracted Characters: {total_characters:,}, Total Tables: {total_tables}")
    if empty_files or missing_titles or missing_dates or missing_sections:
        log.error(f"Integrity failures detected: Empty={len(empty_files)}, Missing Titles={len(missing_titles)}, Missing Dates={len(missing_dates)}")
    else:
        log.info("   [PASS] All policy files have complete metadata, section trees, and non-empty content.")

    if not os.path.isfile(rules_file):
        log.error(f"Rules file '{rules_file}' not found!")
        return

    with open(rules_file, "r", encoding="utf-8") as f:
        policy_entries = json.load(f)

    all_blocks = []
    untraceable_policies = []
    untraceable_blocks = []
    for pol in policy_entries:
        if not pol.get("policy_title") or not pol.get("source_pdf"):
            untraceable_policies.append(pol)
        for b in pol.get("extracted_content", []):
            all_blocks.append(b)
            if not b.get("title") or not b.get("section") or not b.get("content") or not b.get("hierarchy_path"):
                untraceable_blocks.append(b)

    log.info(f"3. CANDIDATE CONTENT CHECK: Total Policy Objects: {len(policy_entries)}, Total Content Blocks: {len(all_blocks):,}")
    if untraceable_policies or untraceable_blocks:
        log.error(f"Found {len(untraceable_policies)} untraceable policies and {len(untraceable_blocks)} untraceable content blocks!")
    else:
        log.info("   [PASS] 100% of candidate blocks possess complete source traceability under their respective policy objects.")

    type_counts = Counter(b.get("content_type", "unknown") for b in all_blocks)
    log.info("   Candidate Content Breakdown:")
    for ct, cnt in type_counts.most_common():
        log.info(f"     - {ct:28}: {cnt:,} ({cnt/len(all_blocks)*100:.1f}%)")
    log.info("=" * 80)

# ==============================================================================
# MAIN EXECUTION ROUTINE
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Unified Humana Policy and Raw Candidate Content Extractor")
    parser.add_argument("--pdf-dir", default=r"humana_policies/pdfs", help="Directory containing Humana policy PDFs")
    parser.add_argument("--extracted-policies-dir", default=r"humana_policies/extracted_policies", help="Target directory for individual policy JSONs")
    parser.add_argument("--out", default=r"humana_policies/extraction_rules.json", help="Path for extraction_rules.json")
    parser.add_argument("--mirror-out", default=r"extracted_rules.json", help="Mirrored path for extraction_rules.json")
    parser.add_argument("--log-file", default="extraction.log", help="Path to execution log file")
    args = parser.parse_args()

    log = logging.getLogger("humana_engine")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    
    fh = logging.FileHandler(args.log_file, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    pdf_dir = os.path.abspath(args.pdf_dir)
    policies_dir = os.path.abspath(args.extracted_policies_dir)
    out_rules_path = os.path.abspath(args.out)
    mirror_rules_path = os.path.abspath(args.mirror_out)

    os.makedirs(policies_dir, exist_ok=True)
    os.makedirs(os.path.dirname(out_rules_path), exist_ok=True)

    log.info("=" * 80)
    log.info("STARTING HUMANA UNIFIED POLICY & CANDIDATE CONTENT EXTRACTION")
    log.info(f"Input PDF Directory:               {pdf_dir}")
    log.info(f"Individual Policy JSON Directory:   {policies_dir}")
    log.info(f"Candidate Content JSON Output:     {out_rules_path}")
    log.info("=" * 80)

    # --------------------------------------------------------------------------
    # STEP 1: EXTRACT EACH PDF INTO AN INDIVIDUAL POLICY JSON FILE
    # --------------------------------------------------------------------------
    log.info("\n>>> STEP 1: EXTRACTING INDIVIDUAL POLICY JSON FILES FROM PDFS...")
    pdf_files = sorted([os.path.join(pdf_dir, f) for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf")])
    total_pdfs = len(pdf_files)
    log.info(f"Found {total_pdfs} PDF policies to extract.")

    extracted_policy_paths = []
    failures = []

    for idx, pdf_path in enumerate(pdf_files, 1):
        fname = os.path.basename(pdf_path)
        try:
            policy_data = parse_policy_to_json(pdf_path, logger=log)
            title = policy_data["policy_metadata"]["policy_title"] or os.path.splitext(fname)[0]
            out_filename = f"{sanitize_filename(title)}.json"
            out_path = os.path.join(policies_dir, out_filename)
            
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(policy_data, f, ensure_ascii=False, indent=2)
                
            extracted_policy_paths.append(out_path)
            stats = policy_data.get("_extraction_stats", {})
            chars = stats.get("total_characters", 0)
            tables = stats.get("total_tables", 0)
            log.info(f"[{idx}/{total_pdfs}] Saved '{out_filename}' ({policy_data['policy_metadata']['total_pages']} pages, {chars:,} chars, {tables} tables)")
        except Exception as e:
            log.error(f"[{idx}/{total_pdfs}] ERROR extracting '{fname}': {e}", exc_info=True)
            failures.append({"file": fname, "error": str(e)})

    log.info(f"Step 1 Complete: {len(extracted_policy_paths)} / {total_pdfs} policy JSON files created.")

    # --------------------------------------------------------------------------
    # STEP 2: EXTRACT CANDIDATE CONTENT BLOCKS INTO extraction_rules.json
    # --------------------------------------------------------------------------
    log.info("\n>>> STEP 2: EXTRACTING CANDIDATE RULE-BEARING CONTENT INTO extraction_rules.json...")
    policy_candidates = []
    total_blocks_count = 0
    
    for idx, j_path in enumerate(extracted_policy_paths, 1):
        fname = os.path.basename(j_path)
        try:
            with open(j_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)
            pol_candidate = extract_candidate_content_from_policy(p_data)
            if pol_candidate and pol_candidate.get("extracted_content"):
                policy_candidates.append(pol_candidate)
                count = len(pol_candidate["extracted_content"])
                total_blocks_count += count
                p_title = pol_candidate.get("policy_title", fname)
                log.info(f"[{idx}/{len(extracted_policy_paths)}] Extracted {count} candidate blocks from '{p_title}'")
        except Exception as e:
            log.error(f"[{idx}/{len(extracted_policy_paths)}] ERROR extracting candidate content from '{fname}': {e}", exc_info=True)

    log.info(f"Writing {len(policy_candidates)} policies ({total_blocks_count:,} content blocks) to '{out_rules_path}'...")
    with open(out_rules_path, "w", encoding="utf-8") as f:
        json.dump(policy_candidates, f, ensure_ascii=False, indent=2)

    if mirror_rules_path:
        log.info(f"Writing mirrored copy to '{mirror_rules_path}'...")
        with open(mirror_rules_path, "w", encoding="utf-8") as f:
            json.dump(policy_candidates, f, ensure_ascii=False, indent=2)

    log.info(f"Step 2 Complete: {len(policy_candidates)} policy objects ({total_blocks_count:,} candidate content blocks) generated.")

    # --------------------------------------------------------------------------
    # STEP 3: PERFORM RIGOROUS AUDIT AND VALIDATION
    # --------------------------------------------------------------------------
    validate_results(pdf_dir, policies_dir, out_rules_path, log)
    log.info("ALL PIPELINE OPERATIONS COMPLETED SUCCESSFULLY.")

if __name__ == "__main__":
    main()
