"""Adapter: records with a guideline document, Q&A rules and linked attachments.

Shape (observed in the UnitedHealthcare extraction; nothing payer-specific here):
  {policy_title, policy_number, effective_date, expiry_date, Line_of_Business,
   Billingclass, CPT_CODES_USED[],
   reimbursement_guidelines: {title, content, tables[{type, content[{key,value}]}],
                              subsections[{title, content, tables[...]}]},
   Question_Answer_Rules: [{"Rule n": {Q, A}}],
   linked_pdf_contents: [{url, content[{page, content}], tables, CPT_CODES_USED[], ICD_CODES_USED[]}],
   resources}
Key names are adapter options so a payer with the same shape but different
key spellings needs only a profile change.
"""
from __future__ import annotations

import json

from src.ingest.base import AdapterContext, BaseAdapter
from src.policy.json_parser import Policy

DEFAULT_KEYS = {
    "title": "policy_title", "number": "policy_number", "effective_date": "effective_date",
    "expiry_date": "expiry_date", "line_of_business": "Line_of_Business", "billing_class": "Billingclass",
    "code_list": "CPT_CODES_USED", "guidelines": "reimbursement_guidelines", "qa": "Question_Answer_Rules",
    "attachments": "linked_pdf_contents", "attachment_codes": "CPT_CODES_USED", "attachment_icd": "ICD_CODES_USED",
}


def _table_text(table) -> tuple[str, dict]:
    cells = table.get("content") if isinstance(table, dict) else table
    if not isinstance(cells, list):
        return (json.dumps(cells, ensure_ascii=False) if cells else ""), {}
    parts, row = [], {}
    for c in cells:
        if isinstance(c, dict):
            k, v = str(c.get("key", "")).strip(), str(c.get("value", "")).strip()
            parts.append(f"{k}: {v}" if v else k)
            row[k] = v
        else:
            parts.append(str(c))
    return " | ".join(p for p in parts if p), row


class GuidelinesDocumentAdapter(BaseAdapter):
    name, version = "guidelines_document", "1"

    def detect(self, records):
        if not records or not isinstance(records[0], dict):
            return 0.0
        k = DEFAULT_KEYS
        hits = sum(1 for r in records[:50] if isinstance(r, dict) and isinstance(r.get(k["guidelines"]), dict))
        return hits / min(len(records), 50)

    def parse(self, records, ctx: AdapterContext):
        k = {**DEFAULT_KEYS, **ctx.profile.adapter_options.get("keys", {})}
        out = []
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                ctx.warnings.append(f"$[{i}] is not an object - skipped")
                continue
            secs, seq = [], 0
            g = rec.get(k["guidelines"]) or {}
            gp = f"$[{i}].{k['guidelines']}"
            gtitle = g.get("title") or "Guidelines"

            def add(content, **kw):
                nonlocal seq
                secs.append(self.make_section(ctx, seq, content, **kw))
                seq += 1

            if (g.get("content") or "").strip():
                add(g["content"], pointer=f"{gp}.content", title=gtitle, section=gtitle, role="payment_policy")
            for ti, t in enumerate(g.get("tables") or []):
                text, row = _table_text(t)
                add(text, pointer=f"{gp}.tables[{ti}]", title=gtitle, section=gtitle, content_type="table_row",
                    table_data={"columns": [], "row": row, "headerless": True}, location="policy_table", role="payment_policy")
            for si, sub in enumerate(g.get("subsections") or []):
                st = sub.get("title")
                path = f"{gtitle} > {st}" if st else gtitle
                if (sub.get("content") or "").strip():
                    add(sub["content"], pointer=f"{gp}.subsections[{si}].content", title=st, section=gtitle,
                        subsection=st, hierarchy_path=path, role="payment_policy")
                for ti, t in enumerate(sub.get("tables") or []):
                    text, row = _table_text(t)
                    add(text, pointer=f"{gp}.subsections[{si}].tables[{ti}]", title=st, section=gtitle, subsection=st,
                        hierarchy_path=path, content_type="table_row", table_data={"columns": [], "row": row, "headerless": True},
                        location="policy_table", role="payment_policy")
            for qi, rule in enumerate(rec.get(k["qa"]) or []):
                for rname, qa in (rule.items() if isinstance(rule, dict) else []):
                    if isinstance(qa, dict):
                        add(f"Q: {qa.get('Q', '')} A: {qa.get('A', '')}", pointer=f"$[{i}].{k['qa']}[{qi}].{rname}",
                            title=rname, section="Questions and Answers", hierarchy_path=f"Q&A > {rname}",
                            location="policy_qa", role="qa")
            lists = []
            if rec.get(k["code_list"]):
                lists.append({"field": k["code_list"], "pointer": f"$[{i}].{k['code_list']}", "location": "code_list_field",
                              "system_hint": None, "codes": [str(c) for c in rec[k["code_list"]]]})
            for ai, att in enumerate(rec.get(k["attachments"]) or []):
                ap, url = f"$[{i}].{k['attachments']}[{ai}]", att.get("url")
                for pi, page in enumerate(att.get("content") or []):
                    if isinstance(page, dict) and (page.get("content") or "").strip():
                        add(page["content"], pointer=f"{ap}.content[{pi}]", title=f"Attachment {ai + 1}",
                            section="Attachment", hierarchy_path=f"Attachment > {url}", page_start=page.get("page"),
                            page_end=page.get("page"), location="attachment", role="attachment", attachment_url=url)
                for ti, t in enumerate(att.get("tables") or []):
                    text, row = _table_text(t)
                    if text:
                        add(text, pointer=f"{ap}.tables[{ti}]", title=f"Attachment {ai + 1}", section="Attachment",
                            hierarchy_path=f"Attachment > {url}", content_type="table_row",
                            table_data={"columns": [], "row": row, "headerless": True}, location="attachment",
                            role="attachment", attachment_url=url)
                if att.get(k["attachment_codes"]):
                    lists.append({"field": f"{k['attachments']}.{k['attachment_codes']}", "pointer": f"{ap}.{k['attachment_codes']}",
                                  "location": "code_list_field", "system_hint": None, "attachment_url": url,
                                  "codes": [str(c) for c in att[k["attachment_codes"]]]})
                if att.get(k["attachment_icd"]):
                    lists.append({"field": f"{k['attachments']}.{k['attachment_icd']}", "pointer": f"{ap}.{k['attachment_icd']}",
                                  "location": "code_list_field", "system_hint": "ICD10CM", "attachment_url": url,
                                  "codes": [str(c) for c in att[k["attachment_icd"]]]})
            number = rec.get(k["number"])
            meta = {kk: vv for kk, vv in rec.items() if kk not in (k["guidelines"], k["qa"], k["attachments"], k["code_list"])}
            p = Policy(policy_uid="", payer="", payer_source="", policy_number=str(number) if number else None,
                       policy_title=rec.get(k["title"]), effective_date=rec.get(k["effective_date"]),
                       line_of_business=rec.get(k["line_of_business"]), billing_class=rec.get(k["billing_class"]),
                       source_pdf=None, sections=secs, content_sha256="", raw_metadata=meta,
                       upstream_code_lists=lists, source_pointer=f"$[{i}]", adapter=f"{self.name}/{self.version}")
            out.append(p)
        return out
