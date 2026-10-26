# Adding a new payer

The engine (mapping, enrichment, matching) never changes. A payer is added with a **profile**
in `config/payers/<payer>.json`, and only exceptionally with a new adapter.

## 1. Inspect the file
`python -c "import json;d=json.load(open('data/input/<file>.json'));print(type(d).__name__, list(d[0]) if isinstance(d,list) else list(d))"`

## 2. Pick an adapter
| Your JSON looks like | adapter |
|---|---|
| a list of policies, each with a list of extracted sections (`extracted_content`, `sections`, ...) | `sectioned_list` |
| a list of policies with a guideline document (`content`/`subsections`/`tables`), Q&A rules, linked attachments, code lists | `guidelines_document` (key names configurable in `adapter_options.keys`) |
| anything else | `declarative` - describe the paths in the profile (no code) |

## 3. Write the profile
```json
{
  "payer": "New Payer",
  "adapter": "declarative",
  "detection_patterns": ["\\bNew Payer\\b"],
  "id_fields": ["policy_number"],
  "generic_title_patterns": ["^Provider Bulletin\\b"],
  "section_role_patterns": {"definition": "\\bdefinitions?\\b"},
  "adapter_options": {
    "records": "payload.documents",
    "fields": {"policy_title": "meta.name", "policy_number": "meta.doc_id", "effective_date": "meta.dates.effective",
               "line_of_business": "meta.product", "billing_class": "meta.claim_type", "source_pdf": "meta.file"},
    "sections": [
      {"each": "body.paragraphs", "text": "text", "title": "heading", "location": "policy_text", "role": "payment_policy"},
      {"each": "body.code_table", "columns": ["code", "description"], "location": "policy_table"},
      {"each": "faq", "text": "answer", "title": "question", "location": "policy_qa", "role": "qa"}
    ],
    "code_lists": [{"path": "billing.codes"}, {"path": "billing.icd_codes", "system_hint": "ICD10CM"}]
  }
}
```
`location` must be one of `policy_text`, `policy_table`, `policy_qa`, `attachment` - it decides the
code provenance class. `id_fields` lists canonical fields usable as stable ids (`source_pdf`,
`policy_number`); records without them get a content-hash id and the `missing_identifier` flag.
A complete working example: `tests/fixtures/payers/acme_health_profile.json`.

## 4. Run
```powershell
python scripts\map_policy_cpt_codes.py --input data\input\new_payer.json --payer-profile new_payer
python scripts\compare_policies.py
```
Check `data\exports\audit\source_documents.csv` (schema report) and `extraction_audit.csv`.

## 5. Only if the declarative adapter cannot describe the shape
Add a class in `src/ingest/adapters/` implementing `detect()` and `parse()` (see `BaseAdapter`) and
register it in `src/ingest/registry.py`. Do not put payer names in it - they belong to the profile.
