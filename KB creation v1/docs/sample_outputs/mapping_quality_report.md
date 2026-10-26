# Mapping quality report

UMLS release in KB: 2026AA
Policies processed: 264
Policies with explicit CPT/HCPCS codes: 133 (50.4%)
Policies with inferred codes: 15 (5.7%)
Policies with NO explicit code but inferred codes: 4 (1.5%)
Policies flagged no_procedure_evidence: 67 (25.4%)

## Confidence distribution

| tier | mappings | needs review |
|---|---|---|
| A | 3 | 0 |
| B | 31 | 31 |
| C | 39685 | 39685 |
| D | 14 | 14 |

## Evidence by method

| method | evidence rows |
|---|---|
| explicit_code | 239739 |
| descriptor_head | 144 |
| exact_synonym | 29 |
| cross_vocab_synonym | 4 |
| normalized_phrase | 3 |
| related_concept | 1 |

Ambiguous mappings: 17
Mappings with title/content conflict: 0
Distinct unmatched procedure-like phrases: 785
Candidate policy pairs: 47

## Review decisions


## Runs

| run | type | status | release | started |
|---|---|---|---|---|
| compare-20261013T180925-8ef26f | compare | succeeded | 2026AA | 2026-10-13 18:09:25.214103 |
| map-20261013T180909-84c670 | map | succeeded | 2026AA | 2026-10-13 18:09:09.474126 |
| map-20261013T180803-7d04c6 | map | succeeded | 2026AA | 2026-10-13 18:08:03.469608 |
| map-20261013T180755-77b32f | map | succeeded | 2026AA | 2026-10-13 18:07:55.125264 |
| kb_build-20261013T180753-6a74be | kb_build | succeeded | 2026AA | 2026-10-13 18:07:53.883704 |

_All figures are counts from the database. Precision/recall appear only in validation_summary.json when a labeled gold file was supplied._