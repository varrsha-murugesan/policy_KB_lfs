"""Parse policy extraction JSON into normalized Policy objects.

Observed real structure (extraction_rules.json, 114 Humana policies):

    [ { "policy_title", "policy_number", "effective_date", "source_pdf",
        "line_of_business", "billing_class",
        "extracted_content": [ { "title", "section", "subsection", "hierarchy_path",
                                 "page_number", "page_start", "page_end",
                                 "content", "codes": [str], "table_data": {columns,row}|null,
                                 "content_type": paragraph|bullet_point|definition|table_row } ] } ]

Facts that shaped this parser:
* There is NO payer field. Payer is taken from --payer, else detected from
  the text (and recorded with payer_source so the provenance is explicit).
* policy_number is NOT unique (CP2013101 and LAB2023027 each label two
  different policies), so the stable key uses source_pdf.
* The upstream 'codes' list mixes CPT, HCPCS, modifiers ("Modifier-KX"),
  revenue codes ("RevCode-0171"), ICD-10, DRG and false positives such as
  "X12", "J01", "B12". It is kept verbatim but treated as unvalidated hints.

Tolerated variants: root dict wrapping a list ("policies", "data", ...), a
single policy object, JSON Lines, and common field-name aliases.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from src.utils.ids import json_sha256, slug, stable_id

log = logging.getLogger(__name__)

TITLE_KEYS = ("policy_title", "title", "policy_name", "name")
NUMBER_KEYS = ("policy_number", "policy_id", "policy_no", "number", "id")
PAYER_KEYS = ("payer", "payer_name", "client", "client_name", "plan_name")
SECTIONS_KEYS = ("extracted_content", "sections", "contents", "content", "rules", "extraction_rules")
WRAPPER_KEYS = ("policies", "data", "items", "results", "documents")
TEXT_KEYS = ("content", "text", "body", "value")

class PolicyParseError(ValueError):
    pass


@dataclass
class Section:
    seq: int
    title: str | None
    section: str | None
    subsection: str | None
    hierarchy_path: str | None
    page_start: int | None
    page_end: int | None
    content_type: str | None
    content: str
    upstream_codes: list[str]
    table_data: dict | None
    section_role: str
    scope_label: str | None
    source_pointer: str | None = None          # JSON path in the source file
    evidence_location: str = "policy_text"     # policy_text | policy_table | policy_qa | attachment
    attachment_url: str | None = None


@dataclass
class Policy:
    policy_uid: str
    payer: str
    payer_source: str
    policy_number: str | None
    policy_title: str | None
    effective_date: str | None
    line_of_business: str | None
    billing_class: str | None
    source_pdf: str | None
    sections: list[Section]
    content_sha256: str
    raw_metadata: dict
    quality_flags: list[str] = field(default_factory=list)
    # schema v2 (payer-agnostic ingestion)
    upstream_code_lists: list[dict] = field(default_factory=list)  # {field, pointer, location, system_hint, codes}
    source_pointer: str | None = None
    adapter: str | None = None
    record_status: str = "ok"
    title_quality: str | None = None
    identifier_quality: str | None = None
    source_document_id: str | None = None
    policy_class: str = "standard"
    n_distinct_codes: int = 0

    @property
    def full_text(self) -> str:
        return "\n".join(s.content for s in self.sections if s.content)


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def section_role(section: str | None, content_type: str | None) -> str:
    s = (section or "").lower()
    if content_type == "definition" or "definition" in s:
        return "definition"
    if s.startswith("overview"):
        return "overview"
    if "local coverage" in s or re.search(r"\blcds?\b", s) or "national coverage" in s:
        return "reference"
    if "payment policy" in s or "specific requirements" in s or "requirements" in s:
        return "payment_policy"
    return "other"


def scope_label(section: str | None) -> str | None:
    if not section:
        return None
    m = re.match(r"(.+?)\s+(payment policy|specific requirements)$", section.strip(), re.I)
    return m.group(1).strip() if m else None


def detect_payer(text: str) -> tuple[str | None, dict]:
    """Detect a payer from text using the detection_patterns of the configured
    payer profiles (config/payers/*.json). No payer names live in code."""
    from src.ingest.profiles import load_profiles

    counts = {p.payer: p.detection_hits(text) for p in load_profiles()}
    counts = {k: v for k, v in counts.items() if v}
    if not counts:
        return None, counts
    best = max(sorted(counts), key=counts.get)
    second = sorted(counts.values(), reverse=True)[1] if len(counts) > 1 else 0
    return (best if counts[best] >= 2 * max(second, 1) or second == 0 else None), counts


def load_json_records(path: Path) -> list[dict]:
    if not path.exists():
        raise PolicyParseError(f"Input file not found: {path}. Place your extraction JSON there or pass --input.")
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:  # JSON Lines
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise PolicyParseError(f"{path} is neither JSON nor JSON Lines: {exc}") from exc
    if isinstance(data, dict):
        for k in WRAPPER_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
        if _first(data, TITLE_KEYS) or _first(data, SECTIONS_KEYS):
            return [data]
        raise PolicyParseError(f"Unrecognized JSON object with keys {sorted(data)[:15]}; expected a list of policies "
                               f"or one of {WRAPPER_KEYS}.")
    if not isinstance(data, list):
        raise PolicyParseError(f"Unsupported JSON root type {type(data).__name__}")
    return data


def _parse_sections(raw) -> list[Section]:
    if isinstance(raw, str):  # whole policy as one text blob
        raw = [{"content": raw, "content_type": "paragraph"}]
    if not isinstance(raw, list):
        return []
    out = []
    for i, s in enumerate(raw):
        if isinstance(s, str):
            s = {"content": s}
        if not isinstance(s, dict):
            continue
        content = _first(s, TEXT_KEYS)
        content = content if isinstance(content, str) else ("" if content is None else json.dumps(content))
        codes = s.get("codes") or []
        codes = [str(c) for c in codes] if isinstance(codes, list) else [str(codes)]
        section_name = s.get("section") or s.get("heading")
        table = s.get("table_data") if isinstance(s.get("table_data"), dict) else None
        out.append(Section(
            seq=i, title=s.get("title"), section=section_name, subsection=s.get("subsection"),
            hierarchy_path=s.get("hierarchy_path") or section_name,
            page_start=s.get("page_start", s.get("page_number")), page_end=s.get("page_end", s.get("page_number")),
            content_type=s.get("content_type"), content=content, upstream_codes=codes, table_data=table,
            section_role=section_role(section_name, s.get("content_type")), scope_label=scope_label(section_name),
        ))
    return out


def parse_policies(path: Path, payer: str | None = None, profile=None) -> list[Policy]:
    """Backward-compatible entry point. Delegates to src.ingest.loader, which
    selects a payer profile and adapter. `payer` may be a profile name/payer name
    or any free name (then the structure is auto-detected)."""
    from src.ingest.loader import load_policies

    policies, _doc = load_policies(Path(path), payer=payer, profile=profile)
    return policies
