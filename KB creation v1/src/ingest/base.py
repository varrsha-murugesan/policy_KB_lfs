"""Adapter interface and payer-agnostic record utilities.

Adapters turn one payer's JSON records into the common model (Policy /
Section from src.policy.json_parser). They only translate structure; identity,
title quality, record status and code-list handling are decided here, once,
for every payer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.ingest.profiles import PayerProfile
from src.policy.json_parser import Policy, Section, scope_label as default_scope_label, section_role as default_role
from src.utils.ids import json_sha256, slug, stable_id
from src.utils.text_normalizer import normalize

ADAPTER_API_VERSION = "1"
# A title is generic when nothing remains after removing dates, payer names,
# generic words and these administrative words.
_TITLE_NOISE = {"reimbursement", "payment", "policy", "policies", "guideline", "guidelines", "medicare", "advantage",
                "commercial", "medicaid", "products", "plan", "plans", "and", "the", "of", "for", "update", "updates",
                "bulletin", "revised", "effective"}


@dataclass
class AdapterContext:
    profile: PayerProfile
    source_document_id: str
    warnings: list[str] = field(default_factory=list)


class BaseAdapter:
    name = "base"
    version = "1"

    def detect(self, records: list) -> float:
        """0..1 confidence that this adapter understands the records (structure only)."""
        raise NotImplementedError

    def parse(self, records: list, ctx: AdapterContext) -> list[Policy]:
        raise NotImplementedError

    # helpers shared by adapters ------------------------------------------------
    def make_section(self, ctx: AdapterContext, seq: int, content: str | None, *, pointer: str, title=None,
                     section=None, subsection=None, hierarchy_path=None, page_start=None, page_end=None,
                     content_type="paragraph", upstream_codes=None, table_data=None, location="policy_text",
                     role=None, attachment_url=None) -> Section:
        prof = ctx.profile
        r = role or role_for(section or title, content_type, prof)
        return Section(seq=seq, title=title, section=section, subsection=subsection,
                       hierarchy_path=hierarchy_path or section or title, page_start=page_start, page_end=page_end,
                       content_type=content_type, content=content or "", upstream_codes=list(upstream_codes or []),
                       table_data=table_data, section_role=r, scope_label=scope_for(section, prof),
                       source_pointer=pointer, evidence_location=location, attachment_url=attachment_url)


def role_for(name: str | None, content_type: str | None, prof: PayerProfile) -> str:
    for role, rx in (prof.section_role_patterns or {}).items():
        if name and re.search(rx, name, re.I):
            return role
    return default_role(name, content_type)


def scope_for(section: str | None, prof: PayerProfile) -> str | None:
    if prof.scope_label_pattern and section:
        m = re.match(prof.scope_label_pattern, section.strip(), re.I)
        return m.group(1).strip() if m else None
    return default_scope_label(section)


def title_quality(title: str | None, prof: PayerProfile) -> str:
    if not title or not title.strip():
        return "missing"
    for rx in prof.generic_title_patterns:
        if re.search(rx, title, re.I):
            return "generic"
    t = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}\b", " ", title)
    for p_rx in prof.detection_patterns:
        t = re.sub(p_rx, " ", t)
    rest = [w for w in normalize(t).split() if w not in _TITLE_NOISE and not w.isdigit()]
    return "specific" if rest else "generic"


def finalize_records(policies: list[Policy], prof: PayerProfile, payer_source: str = "profile") -> list[str]:
    """Assign deterministic, collision-resistant ids, title quality, record status.
    Returns warnings. Ids: '<payer-slug>:<hash12>' of the first available id field
    (unchanged from schema v1 for source_pdf-keyed payers); otherwise a content
    hash ('c-' prefix). Collisions get an ordinal suffix and are flagged."""
    warnings, seen = [], {}
    ps = slug(prof.payer)
    for p in policies:
        p.payer, p.payer_source = prof.payer, payer_source
        anchor, quality = None, None
        for f in prof.id_fields:
            v = getattr(p, f, None) or p.raw_metadata.get(f)
            if v not in (None, ""):
                anchor, quality = str(v), "source_id"
                break
        if anchor is not None:
            base = f"{ps}:{stable_id(anchor, length=12)}"
        else:
            content_key = json_sha256({"title": p.policy_title, "sections": [s.content for s in p.sections],
                                       "lists": [l["codes"] for l in p.upstream_code_lists],
                                       "meta": {k: v for k, v in sorted(p.raw_metadata.items()) if isinstance(v, (str, int, float))}})
            base, quality = f"{ps}:c-{content_key[:12]}", "content_hash"
            p.quality_flags.append("missing_identifier")
        n = seen.get(base, 0) + 1
        seen[base] = n
        if n > 1:
            p.policy_uid = f"{base}-{n}"
            quality += "_duplicate"
            p.quality_flags.append("duplicate_record_identity")
            warnings.append(f"record {p.source_pointer}: identity {base} repeated; assigned {p.policy_uid}")
        else:
            p.policy_uid = base
        p.identifier_quality = quality
        p.title_quality = title_quality(p.policy_title, prof)
        if p.title_quality != "specific":
            p.quality_flags.append(f"title_{p.title_quality}")
        has_text = any((s.content or "").strip() for s in p.sections)
        if not has_text:
            p.record_status = "extraction_failure"
            p.quality_flags.append("empty_record" if not p.upstream_code_lists else "code_list_only_record")
        else:
            p.record_status = "ok"
    return warnings
