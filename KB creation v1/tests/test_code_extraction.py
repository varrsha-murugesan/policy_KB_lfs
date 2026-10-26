"""Stage 1 - explicit code extraction (Scenario A and false-positive controls)."""
from src.policy.code_extractor import CodeReference, extract_from_text


def ref():
    return CodeReference({"CPT": ["00091", "00092", "00093", "00099"], "HCPCS": ["A0002", "A0003"]})


def by(ms, system):
    return {m.code: m for m in ms if m.code_system == system}


def test_scenario_a_explicit_code_validated_with_context():
    ms = extract_from_text("p", 1, "Providers should report CPT code 00099 for the synthetic service.", ref())
    m = by(ms, "CPT")["00099"]
    assert m.validation_status == "validated_in_kb" and m.detection_rule == "code_context" and m.mappable
    assert "CPT code 00099" in m.context_snippet and m.raw_text == "00099"


def test_bare_numbers_are_not_codes():
    ms = extract_from_text("p", 1, "Mail to PO Box 1200, Louisville KY 40201. Amount $12,345.", ref())
    cpt = by(ms, "CPT")
    assert "40201" in cpt and cpt["40201"].detection_rule == "bare_numeric"
    assert cpt["40201"].validation_status == "not_found_in_kb" and not cpt["40201"].mappable
    assert "12345" not in cpt


def test_unvalidated_context_code_without_reference_is_kept():
    ms = extract_from_text("p", 1, "Report CPT code 45378.", CodeReference({}))
    m = by(ms, "CPT")["45378"]
    assert m.validation_status == "no_reference_loaded" and m.mappable


def test_range_expanded_only_against_reference():
    ms = extract_from_text("p", 1, "Report codes 00091 - 00093 for manipulation.", ref())
    assert sorted(m.code for m in ms if m.from_range == "00091-00093") == ["00091", "00092", "00093"]
    ms2 = extract_from_text("p", 1, "Report codes 98940 - 98942.", CodeReference({}))
    assert {m.detection_rule for m in ms2 if m.code_system == "CPT"} == {"range_endpoint_not_expanded"}


def test_hcpcs_modifiers_revenue_icd_drg():
    text = ("Bill A0002 with modifiers 54, 55 and 56. Modifiers X{EPSU} apply. Revenue code 0171 is used. "
            "Diagnosis P22.0 qualifies. DRG code: N01.")
    ms = extract_from_text("p", 1, text, ref())
    assert by(ms, "HCPCS_L2")["A0002"].validation_status == "validated_in_kb"
    assert set(by(ms, "MODIFIER")) >= {"54", "55", "56", "XE", "XP", "XS", "XU"}
    assert "0171" in by(ms, "REVENUE") and "P22.0" in by(ms, "ICD10CM") and "N01" in by(ms, "DRG")


def test_upstream_codes_field_false_positives_are_unclassified():
    ms = extract_from_text("p", 1, "Electronic claims use the X12 standard.", ref(), upstream=["X12", "Modifier-KX", "RevCode-0170"])
    assert by(ms, "UNCLASSIFIED")["X12"].mappable is False
    assert "KX" in by(ms, "MODIFIER") and "0170" in by(ms, "REVENUE")


def test_cpt_maaa_and_pla_formats():
    ms = extract_from_text("p", 1, "Codes 0012M and 0165U are listed.", CodeReference({}))
    assert {"0012M", "0165U"} <= set(by(ms, "CPT"))


def test_table_code_column():
    table = {"columns": ["Procedure Code", "Description"], "row": {"Procedure Code": "00099", "Description": "x"}}
    ms = extract_from_text("p", 1, "[T] Procedure Code: 00099 | Description: x", ref(), table=table)
    assert by(ms, "CPT")["00099"].detection_rule == "table_code_column"
