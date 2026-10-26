"""Payer-agnostic ingestion, provenance, enrichment, matching robustness, determinism."""
import json
import random
from pathlib import Path

import pytest

from conftest import ROOT, run_map, uhc_like
from src.comparison.policy_matcher import match_policies
from src.ingest.loader import load_policies
from src.ingest.profiles import PayerProfile
from src.policy.json_parser import PolicyParseError
from src.utils.ids import file_sha256

FIX = ROOT / "tests" / "fixtures" / "payers"
ACME_PROFILE = FIX / "acme_health_profile.json"
ACME_FILE = FIX / "acme_health_extracted_contents.json"
REAL = {"humana": ROOT / "data/input/humana_extracted_contents.json",
        "unitedhealthcare": ROOT / "data/input/unitedhealthcare_extracted_contents.json"}


def _origins(con, payer):
    return dict(con.execute("""SELECT m.code, m.mapping_origin FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
                               WHERE p.payer = ?""", [payer]).fetchall())


# ----------------------------------------------------------- third payer ---
def test_third_payer_integrates_through_profile_only(kb_con):
    engine_files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "config").rglob("*"))
    assert not any("acme" in f.read_text(errors="ignore").lower() for f in engine_files if f.is_file())
    run_map(kb_con, ACME_FILE, profile=str(ACME_PROFILE))
    rows = kb_con.execute("""SELECT policy_number, policy_title, record_status, title_quality, identifier_quality, adapter
                             FROM policies WHERE payer='Acme Health' ORDER BY source_pointer""").fetchall()
    assert len(rows) == 3
    assert rows[0][:5] == ("ACME-001", "Care Coordination Program", "ok", "specific", "source_id")
    assert rows[2][2:5] == ("extraction_failure", "generic", "content_hash") and rows[2][5].startswith("declarative")
    o = _origins(kb_con, "Acme Health")
    assert o["00021"].startswith("explicit") and o["00001"].startswith("explicit")
    locs = {r[0] for r in kb_con.execute("SELECT DISTINCT evidence_location FROM policy_sections s JOIN policies p USING (policy_uid) WHERE p.payer='Acme Health'").fetchall()}
    assert locs == {"policy_text", "policy_table", "policy_qa"}
    ptr = kb_con.execute("""SELECT s.source_pointer FROM policy_sections s JOIN policies p USING (policy_uid)
                            WHERE p.policy_number='ACME-001' AND s.evidence_location='policy_qa'""").fetchone()[0]
    assert ptr == "$.payload.documents[0].faq[0]"


def test_third_payer_matches_other_payers(kb_con, humana_file, uhc_file):
    run_map(kb_con, humana_file, "Humana")
    run_map(kb_con, ACME_FILE, profile=str(ACME_PROFILE))
    pairs = match_policies(kb_con, "t")
    acme = [p for p in pairs if "Acme Health" in (p["payer_a"], p["payer_b"])]
    assert acme, "the new payer must be matchable without engine changes"
    titles = dict(kb_con.execute("SELECT policy_uid, policy_title FROM policies").fetchall())
    cc = [p for p in acme if {titles[p["policy_uid_a"]], titles[p["policy_uid_b"]]} >=
          {"Care Coordination Program", "Billing Requirements for Selected Services"}]
    assert cc and cc[0]["code_similarity"] > 0 and cc[0]["relation_class"] in ("code_overlap", "concept_overlap")
    failed = kb_con.execute("SELECT policy_uid FROM policies WHERE record_status <> 'ok'").fetchall()
    assert not any(p["policy_uid_a"] == f[0] or p["policy_uid_b"] == f[0] for p in pairs for f in failed)


def test_unknown_structure_fails_clearly(tmp_path):
    f = tmp_path / "weird.json"
    f.write_text(json.dumps([{"foo": 1}, {"bar": 2}]))
    with pytest.raises(PolicyParseError):
        load_policies(f, payer="Some New Payer")


def test_profile_adapter_mismatch_falls_back_to_structure(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps([{"policy_title": "Acupuncture", "source_pdf": "a.pdf",
                              "extracted_content": [{"content": "Acupuncture is covered.", "section": "Coverage"}]}]))
    prof = PayerProfile(payer="Payer Z", adapter="guidelines_document", id_fields=["source_pdf"])
    pols, doc = load_policies(f, profile=prof)
    assert doc["adapter"].startswith("sectioned_list") and pols[0].sections


# ----------------------------------------------------- record quality ------
def test_missing_ids_generic_titles_empty_records_and_duplicates(tmp_path):
    recs = uhc_like([
        {"title": "Acupuncture Policy", "number": "2026R0001A", "content": "Acupuncture is reimbursed."},
        {"title": "01/01/2026 – UnitedHealthcare Medicare Advantage Reimbursement Policy", "number": None, "codes": ["00001"]},
        {"title": "01/01/2026 – UnitedHealthcare Medicare Advantage Reimbursement Policy", "number": None, "codes": ["00001"]},
        {"title": None, "number": None},
    ])
    f = tmp_path / "u.json"
    f.write_text(json.dumps(recs))
    pols, doc = load_policies(f, profile="unitedhealthcare")
    by = [(p.record_status, p.title_quality, p.identifier_quality) for p in pols]
    assert by[0] == ("ok", "specific", "source_id")
    assert by[1] == ("extraction_failure", "generic", "content_hash")
    assert by[2] == ("extraction_failure", "generic", "content_hash_duplicate") and pols[2].policy_uid.endswith("-2")
    assert by[3] == ("extraction_failure", "missing", "content_hash")
    assert "code_list_only_record" in pols[1].quality_flags and "empty_record" in pols[3].quality_flags
    assert len({p.policy_uid for p in pols}) == 4


def test_generic_title_is_not_used_as_evidence(kb_con, tmp_path):
    recs = uhc_like([{"title": "01/01/2026 – UnitedHealthcare Medicare Advantage Reimbursement Policy", "number": "N1",
                      "content": "Claims follow the fee schedule."}])
    f = tmp_path / "g.json"
    f.write_text(json.dumps(recs))
    run_map(kb_con, f, profile="unitedhealthcare")
    assert kb_con.execute("SELECT COUNT(*) FROM mapping_evidence WHERE evidence_source='policy_title'").fetchone()[0] == 0


# ---------------------------------------------------------- provenance -----
def test_provenance_classes_tables_attachments_code_lists_ranges(kb_con, tmp_path):
    recs = uhc_like([{
        "title": "Synthetic Provenance Policy", "number": "P1",
        "content": "Report CPT code 00099 for the service. Codes 00091 - 00093 apply to manipulation.",
        "tables": [{"type": "table", "content": [{"key": "00041", "value": "00042"}]}],
        "qa": [{"Rule 1": {"Q": "Is 00051 covered?", "A": "Yes, CPT code 00051 is covered."}}],
        "attachments": [{"url": "https://example.test/a.pdf", "content": [{"page": 1, "content": "Code list: CPT code 00031"}],
                         "tables": None, "CPT_CODES_USED": ["00031"], "ICD_CODES_USED": ["E11.9"]}],
        "codes": ["00099", "00011", "A0002"]}])
    f = tmp_path / "p.json"
    f.write_text(json.dumps(recs))
    run_map(kb_con, f, profile="unitedhealthcare")
    o = _origins(kb_con, "UnitedHealthcare")
    assert o["00099"].startswith("explicit")                       # text
    assert o["00041"].startswith("explicit")                       # headerless table cell, validated
    assert o["00051"].startswith("supplementary")                  # Q&A only
    assert o["00031"].startswith("supplementary")                  # attachment
    assert o["00011"] == "code_list_only" and o["A0002"] == "code_list_only"
    assert {"00091", "00092", "00093"} <= set(o)                   # range expanded against the KB
    prov = dict(kb_con.execute("SELECT code, code_provenance FROM policy_cpt_mappings").fetchall())
    assert prov["00051"] == "policy_qa" and prov["00031"] == "attachment" and prov["00011"] == "code_list_field"
    tiers = dict(kb_con.execute("SELECT code, confidence_tier FROM policy_cpt_mappings").fetchall())
    assert tiers["00099"] == "A" and tiers["00051"] == "B" and tiers["00011"] == "C"
    icd = kb_con.execute("SELECT code_system, code_location FROM code_mentions WHERE code='E11.9'").fetchone()
    assert icd == ("ICD10CM", "code_list_field")
    assert kb_con.execute("SELECT COUNT(*) FROM policy_cpt_mappings WHERE code='E11.9'").fetchone()[0] == 0


def test_broad_range_without_reference_is_its_own_status(tmp_path):
    from conftest import connect, initialize_schema
    con = connect(tmp_path / "noref.duckdb")
    initialize_schema(con)
    recs = uhc_like([{"title": "Anesthesia Policy", "number": "R1", "content": "Anesthesia codes 00100 - 01999 are subject to this policy."}])
    f = tmp_path / "r.json"
    f.write_text(json.dumps(recs))
    run_map(con, f, profile="unitedhealthcare")
    st = con.execute("SELECT status, reasons FROM section_mapping_status").fetchall()
    assert st == [("broad_code_range", ["00100-01999"])]


def test_description_mismatch_signal(kb_con, tmp_path):
    from conftest import policy, section
    p = [policy("Validation", "H-X", [section("[T] Procedure Code: 00099 | Code Description: Total knee replacement",
                                               ctype="table_row", table={"columns": ["Procedure Code", "Code Description"],
                                                                         "row": {"Procedure Code": "00099",
                                                                                 "Code Description": "Total knee replacement"}})])]
    f = tmp_path / "h.json"
    f.write_text(json.dumps(p))
    run_map(kb_con, f, "Humana")
    flags = kb_con.execute("SELECT flags, needs_review FROM policy_cpt_mappings WHERE code='00099'").fetchone()
    assert "policy_description_mismatch" in flags[0] and flags[1]


# ------------------------------------------------ large lists & matching ---
def _write(tmp_path, name, recs):
    f = tmp_path / name
    f.write_text(json.dumps(recs))
    return f


@pytest.fixture()
def small_code_list_threshold():
    import dataclasses

    from config.settings import SETTINGS
    orig = SETTINGS.matching
    object.__setattr__(SETTINGS, "matching", dataclasses.replace(orig, code_list_policy_min_codes=10))
    yield
    object.__setattr__(SETTINGS, "matching", orig)


def test_large_code_list_does_not_dominate_matching(kb_con, tmp_path, small_code_list_threshold):
    big = [f"{i:05d}" for i in range(1, 60)] + ["00021"]      # 60 listed codes incl. the CCM code
    run_map(kb_con, _write(tmp_path, "u.json", uhc_like([
        {"title": "Add-on Codes Policy", "number": "BIG1", "content": "Add-on codes are listed in the attachment.", "codes": big},
        {"title": "Care Management Policy", "number": "CCM1", "content": "Chronic care management is reimbursed.",
         "codes": ["00021"]}])), profile="unitedhealthcare")
    run_map(kb_con, _write(tmp_path, "h.json", [__import__("conftest").policy(
        "Chronic Care Management", "H-CCM", [__import__("conftest").section("Report CPT code 00021 for chronic care management.")])]),
        "Humana")
    cls = dict(kb_con.execute("SELECT policy_number, policy_class FROM policies").fetchall())
    assert cls["BIG1"] == "code_list_policy"
    pairs = match_policies(kb_con, "t")
    uid = dict(kb_con.execute("SELECT policy_number, policy_uid FROM policies").fetchall())
    get = lambda a, b: next((p for p in pairs if {p["policy_uid_a"], p["policy_uid_b"]} == {uid[a], uid[b]}), None)  # noqa
    real, noisy = get("H-CCM", "CCM1"), get("H-CCM", "BIG1")
    assert real and real["code_similarity"] > 0.9
    assert noisy is None or (noisy["match_tier"] != "strong" and noisy["code_similarity"] < 0.3
                             and "code_list_policy_involved" in noisy["review_reasons"])


def test_title_only_similarity_never_strong(kb_con, tmp_path):
    run_map(kb_con, _write(tmp_path, "u.json", uhc_like([{"title": "Drug Testing Policy", "number": "D1",
                                                           "content": "Presumptive drug screens are reviewed."}])),
            profile="unitedhealthcare")
    run_map(kb_con, _write(tmp_path, "h.json", [__import__("conftest").policy(
        "Drug Testing", "H-D2", [__import__("conftest").section("Definitive drug testing is reviewed.")])]), "Humana")
    for p in match_policies(kb_con, "t"):
        assert p["match_tier"] != "strong" and p["needs_review"]
        if p["relation_class"] == "title_only":
            assert "title_only_support" in p["review_reasons"]


# ---------------------------------------------------------- determinism ----
def _snapshot(con):
    return (con.execute("SELECT mapping_id, code, mapping_origin, confidence_tier, score, flags FROM policy_cpt_mappings ORDER BY 1").fetchall(),
            con.execute("SELECT evidence_id, method, matched_aui, score FROM mapping_evidence ORDER BY 1").fetchall(),
            con.execute("SELECT policy_uid, section_seq, status FROM section_mapping_status ORDER BY 1, 2").fetchall())


def test_input_order_does_not_change_results(tmp_path):
    from conftest import FIXTURE_META, Run, connect, humana_policies, initialize_schema
    from src.umls.rrf_loader import KnowledgeBaseBuilder
    snaps = []
    for k, seed in enumerate([None, 7, 99]):
        con = connect(tmp_path / f"d{k}.duckdb")
        initialize_schema(con)
        with Run(con, "kb_build", umls_release="2026AA") as run:
            KnowledgeBaseBuilder(con, FIXTURE_META, "2026AA", run.run_id).build()
        recs = humana_policies()
        if seed is not None:
            random.Random(seed).shuffle(recs)
        f = tmp_path / f"in{k}.json"
        f.write_text(json.dumps(recs))
        run_map(con, f, "Humana")
        snaps.append(_snapshot(con))
        con.close()
    assert snaps[0] == snaps[1] == snaps[2]


# ------------------------------------------------------------ real files ---
@pytest.mark.parametrize("name", ["humana", "unitedhealthcare"])
def test_real_files_load_and_are_never_modified(name):
    f = REAL[name]
    if not f.exists():
        pytest.skip("real file not present")
    before = file_sha256(f)
    pols, doc = load_policies(f)            # payer detected from the text via profiles
    assert file_sha256(f) == before
    assert doc["payer"] == {"humana": "Humana", "unitedhealthcare": "UnitedHealthcare"}[name]
    assert len({p.policy_uid for p in pols}) == len(pols)
    assert all(s.source_pointer for p in pols for s in p.sections)
    if name == "humana":
        assert len(pols) == 114 and all(p.record_status == "ok" for p in pols)
    else:
        st = [p.record_status for p in pols]
        assert len(pols) == 147 and st.count("extraction_failure") == 50
        assert sum(p.title_quality == "generic" for p in pols) == 49
        assert sum(p.identifier_quality == "source_id" for p in pols) == 97
        locs = {s.evidence_location for p in pols for s in p.sections}
        assert locs == {"policy_text", "policy_table", "policy_qa", "attachment"}


def test_code_list_ids_do_not_depend_on_record_position(tmp_path):
    """Regression: list-entry mention ids once hashed the absolute JSON pointer ($[i]...)."""
    from conftest import FIXTURE_META, Run, connect, initialize_schema
    from src.umls.rrf_loader import KnowledgeBaseBuilder
    recs = uhc_like([{"title": f"Policy {i}", "number": f"N{i}", "content": "Text.", "codes": ["00021", "00031"],
                      "attachments": [{"url": "u", "content": [], "tables": None, "CPT_CODES_USED": ["00001"]}]}
                     for i in range(5)])
    snaps = []
    for k, order in enumerate([recs, list(reversed(recs))]):
        con = connect(tmp_path / f"o{k}.duckdb")
        initialize_schema(con)
        with Run(con, "kb_build", umls_release="2026AA") as run:
            KnowledgeBaseBuilder(con, FIXTURE_META, "2026AA", run.run_id).build()
        f = tmp_path / f"o{k}.json"
        f.write_text(json.dumps(order))
        run_map(con, f, profile="unitedhealthcare")
        snaps.append(con.execute("SELECT mention_id FROM code_mentions ORDER BY 1").fetchall())
        con.close()
    assert snaps[0] == snaps[1]
