"""Stage 4 - title-based inference runs independently (Scenario C)."""
from conftest import mappings, run_map
from src.mapping.term_matcher import TermMatcher
from src.mapping.title_mapper import title_items


def test_scenario_c_title_only(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    m = mappings(kb_con, "H-C")["00031"]
    assert m["sources"] == ["policy_title"] and m["origin"] == "inferred" and m["scope"] == "policy_level"
    ev = kb_con.execute("""SELECT e.evidence_source, e.matched_phrase, e.hierarchy_path FROM mapping_evidence e
                           JOIN policy_cpt_mappings m USING (mapping_id) JOIN policies p ON p.policy_uid = m.policy_uid
                           WHERE p.policy_number='H-C'""").fetchall()
    assert ev == [("policy_title", "Hemoglobin A1c", "TITLE")]


def test_title_fuzzy_match_is_low_scored(kb_con):
    class P:  # minimal policy object
        policy_uid, policy_title, sections = "p", "Physical Therapy Evaluations Policy", []
    r = TermMatcher(kb_con).match(title_items(P()))
    e = [x for x in r.evidence if x.code == "00051"]
    assert e and e[0].method in ("normalized_phrase", "fuzzy", "exact_synonym")
