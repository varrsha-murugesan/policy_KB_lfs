"""Regression tests for corrected UMLS-based CPT/HCPCS mapping logic."""
import pytest
from conftest import kb_con, humana_file, uhc_file
from src.mapping.term_matcher import TermMatcher, TextItem
from src.utils.text_normalizer import is_generic_phrase, normalize


def _match(con, text, source="policy_content"):
    return TermMatcher(con).match([TextItem("test_policy", source, 1, "S", "payment_policy", text, {})])


def test_generic_phrases_blocked(kb_con):
    """Generic phrases must not independently generate CPT/HCPCS mappings."""
    phrases = [
        "In addition, the provider must submit records.",
        "Placement of the device is evaluated.",
        "Preparation of the patient is required.",
        "Standard administration procedures apply.",
        "Surgical procedure guidelines must be followed.",
        "Routine monitoring and care is recommended.",
        "Proper documentation is essential.",
    ]
    for p in phrases:
        r = _match(kb_con, p)
        # None of these generic phrases should produce procedure code mappings
        assert len(r.evidence) == 0, f"Phrase '{p}' unexpectedly produced evidence: {r.evidence}"


def test_is_generic_phrase_detects_all_required_words():
    """Verify that all target generic terms are identified as generic."""
    generic_samples = [
        "placement", "preparation", "addition", "administration",
        "procedure", "treatment", "service", "monitoring",
        "limited", "documentation", "repair", "closure",
        "removal", "replacement", "insertion", "care", "visit",
        "evaluation and management", "surgical procedure",
        "diagnostic tests", "laboratory procedures", "modifiers"
    ]
    for term in generic_samples:
        assert is_generic_phrase(normalize(term)), f"'{term}' was not detected as generic"


def test_clinical_procedures_still_recognized(kb_con):
    """Specific clinical procedures must still be recognized and mapped."""
    r = _match(kb_con, "A screening colonoscopy examines the large intestine.")
    assert any(e.code == "A0001" for e in r.evidence)


def test_ablation_ambiguity_preserved(kb_con):
    """Ambiguous procedure cues (e.g. ablation) are preserved with ambiguous flags."""
    r = _match(kb_con, "Ablation requires prior authorization.")
    assert any(e.code in ("00041", "00042") for e in r.evidence)

