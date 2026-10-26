"""Stage 5 - consolidation, confidence, review and idempotency (Scenarios D, E, L)."""
from datetime import datetime

from conftest import mappings, run_map
from src.mapping.confidence import evidence_score, tier


def test_scenario_d_title_and_content_same_code_one_mapping(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    ms = mappings(kb_con, "H-D")
    m = ms["A0001"]
    assert set(m["sources"]) == {"policy_content", "policy_title"}
    n = kb_con.execute("""SELECT COUNT(*) FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
                          WHERE p.policy_number='H-D' AND m.code='A0001'""").fetchone()[0]
    assert n == 1
    srcs = {r[0] for r in kb_con.execute("""SELECT DISTINCT e.evidence_source FROM mapping_evidence e
            JOIN policy_cpt_mappings m USING (mapping_id) JOIN policies p ON p.policy_uid=m.policy_uid
            WHERE p.policy_number='H-D' AND m.code='A0001'""").fetchall()}
    assert srcs == {"policy_content", "policy_title"}


def test_scenario_e_title_content_disagreement_is_flagged(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    ms = mappings(kb_con, "H-E")
    assert {"00051", "00031"} <= set(ms)
    assert ms["00051"]["conflict"] and ms["00031"]["conflict"]
    flags = kb_con.execute("SELECT quality_flags FROM policies WHERE policy_number='H-E'").fetchone()[0]
    assert "title_content_disagreement" in flags


def test_explicit_validated_is_tier_a_and_distinct_from_inferred(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    ms = mappings(kb_con, "H-A")
    assert ms["00099"]["origin"] == "explicit" and ms["00099"]["tier"] == "A"
    assert "40201" not in ms   # ZIP code never becomes a mapping


def test_scoring_rules():
    assert evidence_score("exact_preferred_term") == 85
    assert evidence_score("descriptor_head", candidate_set_size=2) == 52
    assert evidence_score("exact_synonym", n_tokens=1) == 50
    assert tier(100, True, False, False) == "A" and tier(80, False, False, True) == "C" and tier(20, False, False, False) == "E"


def test_scenario_l_repeated_runs_are_idempotent(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    counts1 = [kb_con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("policies", "policy_cpt_mappings", "mapping_evidence", "code_mentions", "policy_rules")]
    ids1 = set(r[0] for r in kb_con.execute("SELECT mapping_id FROM policy_cpt_mappings").fetchall())
    run_map(kb_con, humana_file, "Humana")
    counts2 = [kb_con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("policies", "policy_cpt_mappings", "mapping_evidence", "code_mentions", "policy_rules")]
    assert counts1 == counts2
    assert ids1 == set(r[0] for r in kb_con.execute("SELECT mapping_id FROM policy_cpt_mappings").fetchall())
    assert kb_con.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0] == counts1[0]
    assert kb_con.execute("SELECT COUNT(*) FROM processing_runs WHERE run_type='map' AND status='succeeded'").fetchone()[0] == 2


def test_review_decisions_persist_and_are_policy_scoped(kb_con, humana_file):
    run_map(kb_con, humana_file, "Humana")
    uid = kb_con.execute("SELECT policy_uid FROM policies WHERE policy_number='H-G'").fetchone()[0]
    kb_con.execute("""INSERT INTO review_decisions VALUES ('d1', ?, 'CPT', '00041', 'reject', 'qa', 'not renal', ?, NULL,
                      '2026AA', TRUE)""", [uid, datetime.now()])
    run_map(kb_con, humana_file, "Humana")
    st = dict(kb_con.execute("""SELECT p.policy_number, m.review_status FROM policy_cpt_mappings m JOIN policies p
             USING (policy_uid) WHERE m.code='00041'""").fetchall())
    assert st["H-G"] == "rejected"
    assert mappings(kb_con, "H-G")["00042"]["review_status"] == "unreviewed"
