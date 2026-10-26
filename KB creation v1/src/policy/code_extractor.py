"""Stage 1 - explicit code extraction.

Every code-like token is recorded as a CodeMention with its system, location,
the rule that detected it and a validation status. Only CPT / HCPCS Level II
mentions can become policy-to-code mappings, and only when:
  * validated in the loaded KB, or
  * no reference vocabulary is loaded AND the token appeared in a code
    context (e.g. "CPT code 77427", a "Procedure Code" table cell, or the
    upstream codes list) - never a bare 5-digit number on its own.
A bare number that the KB does not contain (ZIP code, amount, ID) stays a
mention with status not_found_in_kb and never becomes a mapping.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

import duckdb

from src.utils.ids import stable_id
from src.utils.text_normalizer import basic_clean, normalize

DESC_STOP = {"and", "the", "for", "with", "without", "each", "per", "any", "other", "than"}
DESCRIPTION_MISMATCH_THRESHOLD = 0.25
SYSTEM_SABS = {"CPT": ("CPT", "HCPT"), "HCPCS_L2": ("HCPCS",)}
MAX_RANGE_EXPANSION = 300

_CODE_TOKEN = r"(?:\d{5}|\d{4}[FTUM]|[A-V]\d{4})"
RX_RANGE = re.compile(rf"(?<![\w$#])({_CODE_TOKEN})\s*(?:[-–—−]|through|thru|to)\s*({_CODE_TOKEN})(?![\w%])")
RX_CPT1 = re.compile(r"(?<![\w$#.,/])(\d{5})(?![\w%]|\.\d|,\d{3}|-\d{4})")
RX_CPT_OTHER = re.compile(r"(?<![\w$#])(\d{4}[FTUM])(?!\w)")
RX_HCPCS = re.compile(r"(?<![\w$#])([A-V]\d{4})(?!\w)")
RX_MODIFIER = re.compile(r"(?i:\bmodifiers?)\s*[-:]?\s*((?:\b[A-Z0-9]{2}\b(?:\s*(?:,|and|or|&|/)\s*)?)+)")
RX_MODIFIER_SET = re.compile(r"\b([A-Z])\{([A-Z]{2,6})\}")           # X{EPSU} -> XE XP XS XU
RX_REVENUE = re.compile(r"(?i:\brevenue codes?)\s*:?\s*(0?\d{3,4})\b")
RX_ICD10 = re.compile(r"(?<![\w.])([A-TV-Z]\d{2}\.[0-9A-Z]{1,4})(?![\w.])")
RX_DRG = re.compile(r"(?i:\b(?:MS-)?DRG(?:\s+codes?)?)\s*:?\s*([A-Z]?\d{2,3})\b")
RX_CONTEXT = re.compile(r"(?i)\b(cpt|hcpcs|codes?|procedure|report(ed|ing)?|bill(ed|ing)?|submit|append|use)\b[^.]{0,40}$")


@dataclass
class CodeMention:
    policy_uid: str
    section_seq: int | None
    code_system: str
    code: str
    raw_text: str
    char_start: int | None
    char_end: int | None
    context_snippet: str
    detection_rule: str
    validation_status: str = "not_validated_system"
    from_range: str | None = None
    sab: str | None = None
    code_location: str = "policy_text"   # title | policy_text | policy_table | policy_qa | attachment | code_list_field
    source_pointer: str | None = None
    quality_flags: list = field(default_factory=list)
    policy_description: str | None = None  # description the policy itself gives next to the code (tables)

    @property
    def mention_id(self) -> str:
        if self.code_location == "code_list_field":  # list entries have no offsets: the pointer disambiguates
            # record-relative pointer only: identities must not depend on the record's position in the file
            rel = (self.source_pointer or "").split("].", 1)[-1]
            return stable_id(self.policy_uid, "list", self.section_seq, rel, self.code_system, self.code)
        return stable_id(self.policy_uid, self.section_seq, self.code_system, self.code, self.char_start,
                         self.detection_rule, self.from_range)

    @property
    def mappable(self) -> bool:
        if self.code_system not in SYSTEM_SABS:
            return False
        if self.validation_status == "validated_in_kb":
            return True
        if self.detection_rule == "bare_numeric":
            return False
        return self.validation_status in ("no_reference_loaded", "not_found_in_kb")


class CodeReference:
    """In-memory view of the code vocabularies in the KB (tens of thousands of rows)."""

    def __init__(self, codes_by_sab: dict[str, list[str]], descriptions: dict | None = None):
        self.codes_by_sab = {sab: sorted(set(c)) for sab, c in codes_by_sab.items()}
        self._sets = {sab: set(c) for sab, c in self.codes_by_sab.items()}
        self.descriptions = descriptions or {}   # (sab, code) -> [normalized code-level term strings]

    @classmethod
    def from_db(cls, con: duckdb.DuckDBPyConnection) -> "CodeReference":
        d: dict[str, list[str]] = {}
        for sab, code in con.execute("SELECT sab, code FROM cpt_codes").fetchall():
            d.setdefault(sab, []).append(code)
        desc: dict = {}
        for sab, code, norm in con.execute("SELECT sab, code, str_norm FROM terms WHERE is_code_level ORDER BY aui").fetchall():
            desc.setdefault((sab, code), []).append(norm)
        return cls(d, desc)

    def description_overlap(self, sab: str, code: str, policy_description: str) -> float | None:
        """Share of the policy description's content words found in the best-matching
        official code-level term (0..1). None when no reference strings are loaded."""
        terms = self.descriptions.get((sab, code))
        words = {w for w in normalize(policy_description).split() if len(w) > 2 and w not in DESC_STOP}
        if not terms or len(words) < 2:
            return None
        return max(len(words & set(t.split())) / len(words) for t in terms)

    def loaded(self, system: str) -> bool:
        return any(s in self._sets for s in SYSTEM_SABS.get(system, ()))

    def validate(self, system: str, code: str) -> tuple[str, str | None]:
        if system not in SYSTEM_SABS:
            return "not_validated_system", None
        if not self.loaded(system):
            return "no_reference_loaded", None
        for sab in SYSTEM_SABS[system]:
            if code in self._sets.get(sab, ()):
                return "validated_in_kb", sab
        return "not_found_in_kb", None

    def between(self, system: str, start: str, end: str) -> list[str]:
        out: set[str] = set()
        for sab in SYSTEM_SABS.get(system, ()):
            codes = self.codes_by_sab.get(sab, [])
            lo, hi = bisect.bisect_left(codes, start), bisect.bisect_right(codes, end)
            out.update(c for c in codes[lo:hi] if len(c) == len(start) and c[-1].isdigit() == start[-1].isdigit())
        return sorted(out)


def code_system_of(token: str) -> str | None:
    if re.fullmatch(r"\d{5}|\d{4}[FTUM]", token):
        return "CPT"
    if re.fullmatch(r"[A-V]\d{4}", token):
        return "HCPCS_L2"
    return None


def _snippet(text: str, s: int, e: int, width: int = 60) -> str:
    return text[max(0, s - width): min(len(text), e + width)].replace("\n", " ").strip()


def _table_code_columns(table: dict | None) -> set[str]:
    if not table:
        return set()
    row = table.get("row") or {}
    return {str(v).strip() for k, v in row.items() if v and re.search(r"code", str(k), re.I)}


def extract_from_text(policy_uid: str, section_seq: int | None, text: str, ref: CodeReference,
                      upstream: list[str] | None = None, table: dict | None = None, location: str = "policy_text",
                      pointer: str | None = None, confirm_codes: set | None = None) -> list[CodeMention]:
    t = basic_clean(text)
    upstream = upstream or []
    upstream_set = set(upstream) | (confirm_codes or set())
    table_codes = _table_code_columns(table)
    mentions: list[CodeMention] = []
    used: list[tuple[int, int]] = []

    def overlaps(s, e):
        return any(s < ue and e > us for us, ue in used)

    def add(system, code, s, e, rule, from_range=None):
        mentions.append(CodeMention(policy_uid, section_seq, system, code, t[s:e] if s is not None else code,
                                    s, e, _snippet(t, s, e) if s is not None else "", rule, from_range=from_range,
                                    code_location=location, source_pointer=pointer))

    # 1) ranges (CPT/HCPCS only, both ends same system and shape)
    for m in RX_RANGE.finditer(t):
        a, b = m.group(1), m.group(2)
        sa, sb = code_system_of(a), code_system_of(b)
        if not sa or sa != sb or len(a) != len(b) or a > b:
            continue
        label = f"{a}-{b}"
        members = ref.between(sa, a, b) if ref.loaded(sa) else []
        if members and len(members) <= MAX_RANGE_EXPANSION:
            for c in members:
                add(sa, c, m.start(), m.end(), "range_expansion", label)
        else:
            rule = "range_endpoint_not_expanded"
            add(sa, a, m.start(1), m.end(1), rule, label)
            add(sa, b, m.start(2), m.end(2), rule, label)
        used.append((m.start(), m.end()))

    # 2) single CPT / HCPCS tokens
    for rx in (RX_CPT1, RX_CPT_OTHER, RX_HCPCS):
        for m in rx.finditer(t):
            s, e = m.start(1), m.end(1)
            if overlaps(s, e):
                continue
            code = m.group(1)
            system = code_system_of(code)
            if code in table_codes:
                rule = "table_code_column"
            elif RX_CONTEXT.search(t[max(0, s - 60):s]):
                rule = "code_context"
            elif code in upstream_set:
                rule = "upstream_field_confirmed"
            elif table is not None and table.get("headerless") and system and ref.validate(system, code)[0] == "validated_in_kb":
                rule = "headerless_table_cell"
            else:
                rule = "bare_numeric" if system == "CPT" and code.isdigit() else "pattern_only"
            add(system, code, s, e, rule)
            used.append((s, e))

    # 3) modifiers, revenue codes, ICD-10-CM, DRG
    for m in RX_MODIFIER.finditer(t):
        for tok in re.finditer(r"\b[A-Z0-9]{2}\b", m.group(1)):
            add("MODIFIER", tok.group(0), m.start(1) + tok.start(), m.start(1) + tok.end(), "modifier_phrase")
    for m in RX_MODIFIER_SET.finditer(t):
        for ch in m.group(2):
            add("MODIFIER", m.group(1) + ch, m.start(), m.end(), "modifier_set_notation")
    for m in RX_REVENUE.finditer(t):
        add("REVENUE", m.group(1).zfill(4), m.start(1), m.end(1), "revenue_phrase")
    for m in RX_ICD10.finditer(t):
        add("ICD10CM", m.group(1), m.start(1), m.end(1), "icd10_dotted_pattern")
    for m in RX_DRG.finditer(t):
        add("DRG", m.group(1), m.start(1), m.end(1), "drg_phrase")
    if table and table.get("row"):
        for k, v in table["row"].items():
            if re.fullmatch(r"modifiers?", str(k).strip(), re.I) and v and re.fullmatch(r"[A-Z0-9]{2}", str(v).strip()):
                add("MODIFIER", str(v).strip(), None, None, "table_modifier_column")

    # 4) upstream 'codes' entries not already found in this section's text
    found = {(mm.code_system, mm.code) for mm in mentions}
    for raw in upstream:
        token = raw.strip()
        if token.lower().startswith("modifier-"):
            sys_, code, rule = "MODIFIER", token.split("-", 1)[1], "upstream_field"
        elif token.lower().startswith("revcode-"):
            sys_, code, rule = "REVENUE", token.split("-", 1)[1], "upstream_field"
        elif code_system_of(token):
            sys_, code, rule = code_system_of(token), token, "upstream_field_only"
        elif RX_ICD10.fullmatch(token):
            sys_, code, rule = "ICD10CM", token, "upstream_field"
        else:
            sys_, code, rule = "UNCLASSIFIED", token, "upstream_field_unrecognized"
        if (sys_, code) in found or (sys_ == "UNCLASSIFIED" and ("DRG", token) in found):
            continue
        found.add((sys_, code))
        mentions.append(CodeMention(policy_uid, section_seq, sys_, code, raw, None, None, "", rule,
                                    code_location="code_list_field" if rule != "upstream_field_unrecognized" else location,
                                    source_pointer=pointer))

    # 5) the policy's own description of a code in the same table row (quality signal)
    if table and table.get("row"):
        row = table["row"]
        desc = next((str(v) for k, v in row.items() if v and re.search(r"descr", str(k), re.I)), None)
        if desc:
            for mm in mentions:
                if mm.code_system in SYSTEM_SABS and mm.detection_rule in ("table_code_column", "code_context"):
                    mm.policy_description = desc

    # 6) validation + de-duplication + description cross-check
    out, seen = [], set()
    for mm in mentions:
        key = mm.mention_id
        if key in seen:
            continue
        seen.add(key)
        mm.validation_status, mm.sab = ref.validate(mm.code_system, mm.code)
        if mm.policy_description and mm.validation_status == "validated_in_kb":
            ov = ref.description_overlap(mm.sab, mm.code, mm.policy_description)
            if ov is not None and ov < DESCRIPTION_MISMATCH_THRESHOLD:
                mm.quality_flags.append("policy_description_mismatch")
        out.append(mm)
    return out


def classify_list_code(token: str, system_hint: str | None) -> str:
    if system_hint:
        return system_hint
    return code_system_of(token) or ("ICD10CM" if RX_ICD10.fullmatch(token) else "UNCLASSIFIED")


def extract_code_lists(policy, ref: CodeReference, found_in_text: set) -> list[CodeMention]:
    """Codes supplied through extracted code-list fields. Each entry becomes a
    mention with code_location='code_list_field'; detection_rule tells whether
    the same code was also found in the policy's own text."""
    out, seen = [], set()
    for lst in policy.upstream_code_lists:
        for raw in lst["codes"]:
            code = raw.strip().upper()
            system = classify_list_code(code, lst.get("system_hint"))
            key = (lst["pointer"], system, code)
            if key in seen or not code:
                continue
            seen.add(key)
            rule = "code_list_field_confirmed_in_text" if (system, code) in found_in_text else "code_list_field_only"
            mm = CodeMention(policy.policy_uid, None, system, code, raw, None, None, lst.get("attachment_url") or "",
                             rule, code_location="code_list_field", source_pointer=lst["pointer"])
            mm.validation_status, mm.sab = ref.validate(system, code)
            out.append(mm)
    return out


def extract_policy_codes(policy, ref: CodeReference) -> list[CodeMention]:
    confirm = {c.strip().upper() for lst in getattr(policy, "upstream_code_lists", []) if not lst.get("system_hint")
               for c in lst["codes"]}
    mentions = []
    if policy.policy_title and getattr(policy, "title_quality", "specific") == "specific":
        mentions += extract_from_text(policy.policy_uid, None, policy.policy_title, ref, location="title",
                                      pointer=f"{policy.source_pointer}.title" if policy.source_pointer else None)
    for s in policy.sections:
        mentions += extract_from_text(policy.policy_uid, s.seq, s.content, ref, s.upstream_codes, s.table_data,
                                      location=getattr(s, "evidence_location", "policy_text"),
                                      pointer=getattr(s, "source_pointer", None), confirm_codes=confirm)
    found = {(m.code_system, m.code) for m in mentions}
    return mentions + extract_code_lists(policy, ref, found)
