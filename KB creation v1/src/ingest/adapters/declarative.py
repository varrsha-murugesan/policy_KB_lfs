"""Declarative adapter: a new payer's JSON shape is described in its profile,
so onboarding a payer needs NO code. Example (adapter_options):

  "records": "documents",                       # dotted path to the record list ("" = root list)
  "fields": {"policy_title": "name", "policy_number": "doc_id",
             "effective_date": "dates.effective", "line_of_business": "product",
             "billing_class": "claim_type", "source_pdf": "source.file"},
  "sections": [
     {"each": "body.paragraphs", "text": "text", "title": "heading",
      "location": "policy_text", "role": "payment_policy"},
     {"each": "body.code_table", "columns": ["code", "description"],
      "location": "policy_table"}            # rendered as 'code: X | description: Y'
  ],
  "code_lists": [{"path": "billing.codes", "system_hint": null}]
"""
from __future__ import annotations

from src.ingest.base import AdapterContext, BaseAdapter
from src.policy.json_parser import Policy

CANONICAL_FIELDS = ("policy_title", "policy_number", "effective_date", "line_of_business", "billing_class", "source_pdf")


def get_path(obj, path: str | None):
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


class DeclarativeAdapter(BaseAdapter):
    name, version = "declarative", "1"

    def detect(self, records):
        return 0.0  # never auto-selected: requires an explicit profile

    def records_from(self, data, options):
        recs = get_path(data, options.get("records", "")) if isinstance(data, dict) else data
        return recs if isinstance(recs, list) else []

    def parse(self, records, ctx: AdapterContext):
        o = ctx.profile.adapter_options
        fields, out = o.get("fields", {}), []
        rp = o.get("records") or ""
        for i, rec in enumerate(records):
            base = f"$.{rp}[{i}]" if rp else f"$[{i}]"
            if not isinstance(rec, dict):
                ctx.warnings.append(f"{base} is not an object - skipped")
                continue
            secs, seq = [], 0
            for spec in o.get("sections", []):
                items = get_path(rec, spec["each"])
                items = items if isinstance(items, list) else ([items] if items else [])
                for j, it in enumerate(items):
                    if spec.get("columns"):
                        row = {c: str(get_path(it, c) or "") for c in spec["columns"]}
                        text = " | ".join(f"{c}: {v}" for c, v in row.items() if v)
                        table = {"columns": spec["columns"], "row": row}
                        ctype = "table_row"
                    else:
                        text = get_path(it, spec.get("text")) if isinstance(it, dict) else it
                        table, ctype = None, spec.get("content_type", "paragraph")
                    title = get_path(it, spec["title"]) if spec.get("title") and isinstance(it, dict) else spec.get("section")
                    secs.append(self.make_section(ctx, seq, str(text or ""), pointer=f"{base}.{spec['each']}[{j}]",
                                                  title=title, section=spec.get("section") or title, content_type=ctype,
                                                  table_data=table, location=spec.get("location", "policy_text"),
                                                  role=spec.get("role")))
                    seq += 1
            lists = []
            for cl in o.get("code_lists", []):
                codes = get_path(rec, cl["path"])
                if codes:
                    lists.append({"field": cl["path"], "pointer": f"{base}.{cl['path']}", "location": "code_list_field",
                                  "system_hint": cl.get("system_hint"), "codes": [str(c) for c in codes]})
            vals = {f: get_path(rec, fields[f]) if f in fields else None for f in CANONICAL_FIELDS}
            out.append(Policy(policy_uid="", payer="", payer_source="",
                              policy_number=str(vals["policy_number"]) if vals["policy_number"] else None,
                              policy_title=vals["policy_title"], effective_date=vals["effective_date"],
                              line_of_business=vals["line_of_business"], billing_class=vals["billing_class"],
                              source_pdf=vals["source_pdf"], sections=secs, content_sha256="",
                              raw_metadata={k: v for k, v in rec.items() if isinstance(v, (str, int, float, bool)) or v is None},
                              upstream_code_lists=lists, source_pointer=base, adapter=f"{self.name}/{self.version}"))
        return out
