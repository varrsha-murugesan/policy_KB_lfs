"""Sections 9-10 - cross-payer matching and gap analysis (Scenarios H, I, J) + parser/rules."""
import json

from conftest import mappings, run_map
from src.comparison.policy_matcher import match_policies
from src.comparison.rule_comparator import run_gap_analysis
from src.knowledge_base.db_manager import insert_records
from src.policy.json_parser import parse_policies
from src.policy.rule_extractor import classify_sentence, extract_rules


def _compare(con):
    pairs = match_policies(con, "cmp")
    insert_records(con, "policy_pair_matches", pairs)
    findings = run_gap_analysis(con, pairs, "cmp")
    return {(p["policy_uid_a"], p["policy_uid_b"]): p for p in pairs}, findings


def _uid(con, number):
    return con.execute("SELECT policy_uid FROM policies WHERE policy_number=?", [number]).fetchone()[0]


def _pair(pairs, a, b):
    return pairs.get((a, b)) or pairs.get((b, a))


def test_scenario_h_different_terminology_same_procedure(kb_con, humana_file, uhc_file):
    run_map(kb_con, humana_file, "Humana")
    run_map(kb_con, uhc_file, "UnitedHealthcare")
    assert "00021" in mappings(kb_con, "U-H")           # 'CCM services' resolved through abbreviation
    pairs, _ = _compare(kb_con)
    p = _pair(pairs, _uid(kb_con, "H-B"), _uid(kb_con, "U-H"))
    assert p and p["payer_a"] != p["payer_b"]
    assert "CPT:00021" in p["shared_inferred_codes"] and "C9900021" in p["shared_cuis"]
    assert "shared" in p["explanation"]


def test_scenario_i_shared_code_different_scope(kb_con, humana_file, uhc_file):
    run_map(kb_con, humana_file, "Humana")
    run_map(kb_con, uhc_file, "UnitedHealthcare")
    pairs, findings = _compare(kb_con)
    p = _pair(pairs, _uid(kb_con, "H-I"), _uid(kb_con, "U-I"))
    assert p and "CPT:00031" in p["shared_explicit_codes"]
    fs = [f for f in findings if f["pair_id"] == p["pair_id"]]
    assert any(f["finding_type"] == "differs_in_indication_scope" for f in fs)
    assert not any(f["finding_type"] == "conflict_candidate" for f in fs)


def test_scenario_j_incomplete_extraction_is_not_called_missing(kb_con, humana_file, uhc_file):
    run_map(kb_con, humana_file, "Humana")
    run_map(kb_con, uhc_file, "UnitedHealthcare")
    flags = kb_con.execute("SELECT quality_flags FROM policies WHERE policy_number='U-J'").fetchone()[0]
    assert any(f.startswith("possible_truncated") for f in flags) and "no_payment_policy_section" in flags
    pairs, findings = _compare(kb_con)
    p = _pair(pairs, _uid(kb_con, "H-J"), _uid(kb_con, "U-J"))
    assert p, "acupuncture policies should be paired"
    fs = [f for f in findings if f["pair_id"] == p["pair_id"]]
    hum_side = "b" if p["policy_uid_a"] == _uid(kb_con, "U-J") else "a"
    other = "a" if hum_side == "b" else "b"
    assert not any(f["finding_type"].startswith(f"not_identified_in_{other}") for f in fs)
    assert any(f["finding_type"] == "not_assessable_incomplete_extraction" for f in fs)
    assert all(f["status"] in ("needs_review", "informational") for f in fs)


def test_same_payer_pairs_excluded_by_default(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    assert match_policies(kb_con, "x") == []


def test_rule_types_polarity_and_attributes(humana_file):
    pol = [p for p in parse_policies(humana_file, "Humana") if p.policy_number == "H-J"][0]
    rules = extract_rules(pol)
    lim = [r for r in rules if r.rule_type == "frequency_limit"][0]
    assert lim.polarity == "limit" and "20 session" in lim.attributes["quantities"]
    pa = [r for r in rules if r.rule_type == "prior_authorization"][0]
    assert pa.polarity == "not_required"
    mod = [r for r in rules if "KX" in r.attributes["modifiers"]][0]
    assert mod.polarity == "require"
    assert classify_sentence("Services are not covered when billed with modifier 59.")[0] == "exclusion"


def test_parser_variants_and_payer_detection(tmp_path):
    rec = {"title": "X", "policy_id": "1", "sections": [{"text": "UnitedHealthcare covers it. UnitedHealthcare pays."}]}
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"policies": [rec]}))
    pol = parse_policies(p)[0]
    assert pol.payer == "UnitedHealthcare" and pol.payer_source == "detected" and pol.policy_title == "X"
    jl = tmp_path / "v.jsonl"
    jl.write_text(json.dumps(rec) + "\n")
    assert parse_policies(jl, payer="UHC")[0].payer_source == "cli"


def test_real_file_structure_if_present():
    from pathlib import Path
    import pytest
    f = next((p for p in [Path("data/input/humana_extracted_contents.json"), Path("data/input/extraction_contents.json")]
              if p.exists()), None)
    if f is None:
        pytest.skip("real extraction file not present")
    pols = parse_policies(f)
    assert pols and all(p.policy_uid for p in pols)
    assert len({p.policy_uid for p in pols}) == len(pols)
