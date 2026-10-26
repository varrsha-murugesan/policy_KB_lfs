# Audit summary

- **Acme Health**: 3 records, 2 ok, 1 extraction failures, 1 generic titles, 0 missing titles, 1 without a source identifier, 0 code-list policies
- **Humana**: 114 records, 114 ok, 0 extraction failures, 0 generic titles, 0 missing titles, 0 without a source identifier, 0 code-list policies
- **UnitedHealthcare**: 147 records, 97 ok, 50 extraction failures, 49 generic titles, 1 missing titles, 50 without a source identifier, 13 code-list policies

## Section status

| payer | status | sections |
|---|---|---|
| Acme Health | inferred_candidates | 3 |
| Acme Health | explicit_codes | 3 |
| Acme Health | not_applicable | 1 |
| Humana | explicit_codes | 703 |
| Humana | not_applicable | 540 |
| Humana | unresolved | 127 |
| Humana | inferred_candidates | 12 |
| Humana | broad_code_range | 3 |
| UnitedHealthcare | supplementary_codes | 2952 |
| UnitedHealthcare | not_applicable | 2623 |
| UnitedHealthcare | unresolved | 172 |
| UnitedHealthcare | explicit_codes | 149 |
| UnitedHealthcare | broad_code_range | 91 |
| UnitedHealthcare | inferred_candidates | 10 |

## Mapping provenance

| payer | origin | mappings |
|---|---|---|
| Acme Health | explicit+inferred | 3 |
| Humana | explicit | 459 |
| Humana | inferred | 11 |
| UnitedHealthcare | supplementary | 38182 |
| UnitedHealthcare | explicit | 765 |
| UnitedHealthcare | code_list_only | 300 |
| UnitedHealthcare | inferred | 13 |

## Quality signals

- ambiguous_candidate_set: 17
- explicit_code_not_in_kb_release: 39680
- listed_only_in_code_list_field: 300
- stated_only_in_qa_or_attachment: 38182

## Policy matches

| tier | relation | pairs | needs review |
|---|---|---|---|
| possible | code_overlap | 22 | 22 |
| possible | concept_overlap | 2 | 2 |
| strong | code_overlap | 10 | 1 |
| weak | code_overlap | 1 | 1 |
| weak | concept_overlap | 5 | 5 |
| weak | title_only | 6 | 6 |
| weak | weak_signal | 1 | 1 |