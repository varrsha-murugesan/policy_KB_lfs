"""Stage 2-3 - content-based inference (Scenarios B, F, G) using synthetic terminology."""
from conftest import mappings, run_map
from src.mapping.term_matcher import TermMatcher, TextItem
from src.utils.text_normalizer import bag_key, descriptor_head, find_abbreviations, normalize


def test_normalization_is_deterministic():
    assert normalize("Hemoglobin A1c’s  test–level") == "hemoglobin a1c test level"
    assert bag_key("Testing of hemoglobin") == bag_key("hemoglobin testing")
    assert descriptor_head("Colonoscopy, flexible; with biopsy") == "colonoscopy"
    assert find_abbreviations("Chronic care management (CCM) is billed")["ccm"] == "chronic care management"
    assert find_abbreviations("billed monthly (see table)") == {}


def _match(con, text, source="policy_content", abbr=None):
    return TermMatcher(con).match([TextItem("p", source, 1, "S", "payment_policy", text, abbr or {})])


def test_scenario_b_procedure_name_without_code(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    m = mappings(kb_con, "H-B")["00021"]
    assert m["origin"] == "inferred" and m["best_method"] == "exact_synonym" and m["tier"] == "B"
    ev = kb_con.execute("""SELECT matched_phrase, matched_term, cui, matched_sab, section_seq, abbreviation_expanded
                           FROM mapping_evidence e JOIN policy_cpt_mappings m USING (mapping_id)
                           JOIN policies p ON p.policy_uid = m.policy_uid
                           WHERE p.policy_number = 'H-B' AND m.code = '00021'""").fetchall()
    assert any(e[0].lower() == "chronic care management" and e[2] == "C9900021" and e[3] == "CPT" for e in ev)


def test_scenario_g_ambiguous_phrase_keeps_candidate_set(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    ms = mappings(kb_con, "H-G")
    assert {"00041", "00042"} <= set(ms)
    for c in ("00041", "00042"):
        assert ms[c]["ambiguous"] and "ambiguous_candidate_set" in ms[c]["flags"] and ms[c]["tier"] in ("D", "E")


def test_scenario_f_multiple_procedures_keep_section_scope(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    ms = mappings(kb_con, "H-F")
    assert ms["00021"]["section_seqs"] != ms["00031"]["section_seqs"]
    assert ms["00021"]["scope"] == ms["00031"]["scope"] == "section_level"


def test_cross_vocabulary_synonym_on_same_cui(kb_con):
    r = _match(kb_con, "A screening colonoscopy is covered.")
    codes = {(e.code, e.method) for e in r.evidence}
    assert ("A0001", "cross_vocab_synonym") in codes
    assert not any(e.code in ("00011", "00012") for e in r.evidence)  # nested 'colonoscopy' head is subsumed


def test_related_concept_path_is_recorded(kb_con):
    r = _match(kb_con, "Dry needling is allowed.")
    e = [x for x in r.evidence if x.method == "related_concept"][0]
    assert e.code == "00001" and "-RQ->" in e.relationship_path and e.score < 40


def test_add_on_relationship_is_not_used_for_inference(kb_con):
    r = _match(kb_con, "Acupuncture needle insertion first 15 minutes")
    assert all(e.method != "related_concept" for e in r.evidence)


def test_generic_single_words_do_not_match(kb_con):
    r = _match(kb_con, "These services and procedures require a claim.")
    assert not r.evidence


def test_recognized_concept_without_code_is_kept_as_concept(kb_con):
    r = _match(kb_con, "Hospital observation care is reviewed.")
    assert not r.evidence and any(c["cui"] == "C9900070" and not c["has_code_link"] for c in r.concepts)


def test_unmatched_procedure_phrases_are_reported(kb_con):
    r = _match(kb_con, "Lithotripsy procedures and nerve conduction testing are reviewed.")
    assert any(u["phrase_norm"].endswith("testing") for u in r.unmatched)
