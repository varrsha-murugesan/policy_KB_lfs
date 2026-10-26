"""Evidence scoring and confidence tiers.

IMPORTANT: scores are ordinal ranking aids derived from fixed, documented
rules. They are NOT probabilities and NOT calibrated estimates of clinical
equivalence. Calibrate thresholds with labeled data (scripts/validate_results.py).

Per-evidence score = BASE[method]
                     - ambiguity penalty  (phrase points to several codes)
                     - context penalty    (reference/definition sections, suppressible terms)
                     capped for single-token phrases.
Mapping score     = max(evidence scores) + corroboration bonus (title AND content agree).

Tiers (mapping level):
  A  explicit code stated in the policy and validated in the loaded KB
  B  explicit code (no reference loaded to validate it), OR inferred with score >= 75 and unambiguous
  C  score >= 55
  D  score >= 35
  E  everything lower (kept, never silently dropped)
"""
from __future__ import annotations

import math

RULESET_VERSION = "conf-1"

BASE = {
    "explicit_code": 100.0,
    "exact_preferred_term": 85.0,   # policy phrase == code's preferred description (PT)
    "exact_synonym": 80.0,          # == another code-level term of the code (SY, ETCLIN, ...)
    "cross_vocab_synonym": 72.0,    # == term from another vocabulary on the SAME CUI as the code
    "normalized_phrase": 68.0,      # same words, different order/plurals
    "descriptor_head": 58.0,        # == leading clause of a code descriptor ("colonoscopy")
    "hierarchy_group": 45.0,        # == a CPT/HCPCS group heading; candidates are its descendants
    "fuzzy": 40.0,                  # multiplied by similarity/100
    "related_concept": 35.0,        # via an allowed UMLS relationship (RQ, SY, RN, CHD)
    "reviewer": 100.0,
}

SINGLE_TOKEN_CAP = 50.0
SECTION_PENALTY = {"reference": 10.0, "definition": 5.0, "qa": 5.0, "attachment": 10.0}
SUPPRESSIBLE_PENALTY = 5.0
CORROBORATION_BONUS = 5.0


def evidence_score(method: str, candidate_set_size: int = 1, n_tokens: int = 2, section_role: str | None = None,
                   suppressible: bool = False, fuzzy_ratio: float | None = None) -> float:
    s = BASE[method]
    if method == "fuzzy" and fuzzy_ratio is not None:
        s = s * fuzzy_ratio / 100.0
    if candidate_set_size > 1 and method not in ("explicit_code", "reviewer"):
        s -= min(25.0, 6.0 * math.log2(candidate_set_size))
    s -= SECTION_PENALTY.get(section_role or "", 0.0)
    if suppressible:
        s -= SUPPRESSIBLE_PENALTY
    if n_tokens == 1 and method not in ("explicit_code", "reviewer"):
        s = min(s, SINGLE_TOKEN_CAP)
    return round(max(s, 1.0), 2)


def tier(score: float, explicit_validated: bool, explicit_unvalidated: bool, ambiguous: bool) -> str:
    if explicit_validated:
        return "A"
    if explicit_unvalidated or (score >= 75 and not ambiguous):
        return "B"
    if score >= 55:
        return "C"
    if score >= 35:
        return "D"
    return "E"
