"""Adapter: records carrying a flat list of extracted sections.

Shape (observed in the Humana extraction, but nothing here is payer-specific):
  {policy_title, policy_number, effective_date, source_pdf, line_of_business,
   billing_class, extracted_content: [{title, section, subsection, hierarchy_path,
   page_start, page_end, content, codes[], table_data{columns,row}, content_type}]}
Field aliases come from src.policy.json_parser so older variants still load.
"""
from __future__ import annotations

from src.ingest.base import AdapterContext, BaseAdapter
from src.policy.json_parser import NUMBER_KEYS, PAYER_KEYS, SECTIONS_KEYS, TEXT_KEYS, TITLE_KEYS, Policy, _first


class SectionedListAdapter(BaseAdapter):
    name, version = "sectioned_list", "1"

    def detect(self, records):
        if not records or not isinstance(records[0], dict):
            return 0.0
        hits = sum(1 for r in records[:50] if isinstance(r, dict) and isinstance(_first(r, SECTIONS_KEYS), (list, str)))
        return hits / min(len(records), 50)

    def parse(self, records, ctx: AdapterContext):
        out = []
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                ctx.warnings.append(f"$[{i}] is not an object - skipped")
                continue
            raw_secs = _first(rec, SECTIONS_KEYS)
            sec_key = next((k for k in SECTIONS_KEYS if k in rec), "extracted_content")
            if isinstance(raw_secs, str):
                raw_secs = [{"content": raw_secs, "content_type": "paragraph"}]
            sections = []
            for j, s in enumerate(raw_secs or []):
                if isinstance(s, str):
                    s = {"content": s}
                if not isinstance(s, dict):
                    continue
                content = _first(s, TEXT_KEYS)
                table = s.get("table_data") if isinstance(s.get("table_data"), dict) else None
                codes = s.get("codes") or []
                sections.append(self.make_section(
                    ctx, j, content if isinstance(content, str) else ("" if content is None else str(content)),
                    pointer=f"$[{i}].{sec_key}[{j}]", title=s.get("title"), section=s.get("section") or s.get("heading"),
                    subsection=s.get("subsection"), hierarchy_path=s.get("hierarchy_path"),
                    page_start=s.get("page_start", s.get("page_number")), page_end=s.get("page_end", s.get("page_number")),
                    content_type=s.get("content_type"), upstream_codes=[str(c) for c in codes] if isinstance(codes, list) else [str(codes)],
                    table_data=table, location="policy_table" if table else "policy_text"))
            number = _first(rec, NUMBER_KEYS)
            meta = {k: v for k, v in rec.items() if k not in SECTIONS_KEYS}
            p = Policy(policy_uid="", payer="", payer_source="", policy_number=str(number) if number is not None else None,
                       policy_title=_first(rec, TITLE_KEYS), effective_date=rec.get("effective_date"),
                       line_of_business=rec.get("line_of_business"), billing_class=rec.get("billing_class"),
                       source_pdf=rec.get("source_pdf") or rec.get("source_file"), sections=sections,
                       content_sha256="", raw_metadata=meta, source_pointer=f"$[{i}]", adapter=f"{self.name}/{self.version}")
            if _first(rec, PAYER_KEYS):
                p.raw_metadata["payer_in_record"] = _first(rec, PAYER_KEYS)
            out.append(p)
        return out
