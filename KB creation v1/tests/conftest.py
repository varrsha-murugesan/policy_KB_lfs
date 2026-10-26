"""Shared fixtures. All terminology is SYNTHETIC (tests/fixtures/make_fixture.py);
all policies are invented and follow the structure of the real extraction JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.knowledge_base.db_manager import Run, connect, initialize_schema  # noqa: E402
from src.umls.rrf_loader import KnowledgeBaseBuilder  # noqa: E402

FIXTURE_META = ROOT / "tests" / "fixtures" / "META"


def section(content, section="Medicare Advantage Payment Policy", subsection=None, ctype="paragraph", codes=None,
            table=None):
    path = section if not subsection else f"{section} > {subsection}"
    return {"title": subsection or section, "section": section, "subsection": subsection, "hierarchy_path": path,
            "page_number": 1, "page_start": 1, "page_end": 1, "content": content, "codes": codes or [],
            "table_data": table, "content_type": ctype}


def policy(title, number, sections, pdf=None, lob="Medicare Advantage Products", billing="Professional"):
    return {"policy_title": title, "policy_number": number, "effective_date": "01/2026",
            "source_pdf": pdf or f"{number}.pdf", "line_of_business": lob, "billing_class": billing,
            "extracted_content": sections}


OVERVIEW = "This policy describes the plan's billing requirements and reimbursement guidelines."


def humana_policies():
    return [
        # Scenario A: explicit code + description (validated code 00099) and a bare unvalidated number
        policy("Synthetic Validation Service", "H-A", [
            section(OVERVIEW, "Overview"),
            section("Providers should report CPT code 00099 for the synthetic validation service. "
                    "Mail claims to PO Box 1200, Louisville KY 40201.", codes=["00099"]),
        ]),
        # Scenario B: procedure name, no code
        policy("Billing Requirements for Selected Services", "H-B", [
            section(OVERVIEW, "Overview"),
            section("The plan covers chronic care management when documentation supports the time spent. "
                    "Chronic care management (CCM) must be billed once per calendar month."),
        ]),
        # Scenario C: title-only match (body gives nothing)
        policy("Laboratory – Hemoglobin A1c", "H-C", [
            section(OVERVIEW, "Overview"),
            section("Claims are subject to the reimbursement rules described below. Payment follows the fee schedule."),
        ]),
        # Scenario D: title and content agree
        policy("Screening Colonoscopy", "H-D", [
            section("A screening colonoscopy examines the large intestine.", "Overview"),
            section("Humana allows one screening colonoscopy every 10 years for average-risk members. "
                    "Modifier PT must be appended when screening converts to diagnostic."),
        ]),
        # Scenario E: title and content disagree
        policy("Physical Therapy Evaluation", "H-E", [
            section(OVERVIEW, "Overview"),
            section("Only hemoglobin A1c results documented in the record are considered."),
        ]),
        # Scenario F: multiple procedures in different sections
        policy("Outpatient Procedures", "H-F", [
            section(OVERVIEW, "Overview"),
            section("Chronic care management is covered up to 1 time per month.", subsection="Care Management"),
            section("Hemoglobin A1c testing is reimbursed once every 90 days.", subsection="Laboratory"),
        ]),
        # Scenario G: ambiguous procedure name
        policy("Tissue Procedures", "H-G", [
            section(OVERVIEW, "Overview"),
            section("Ablation requires prior authorization."),
        ]),
        # Scenario I (Humana side): shared code, screening purpose
        policy("Laboratory – Glycated Hemoglobin Screening", "H-I", [
            section(OVERVIEW, "Overview"),
            section("Screening with CPT code 00031 is reimbursable once every 3 years for adults."),
        ]),
        # Scenario J (Humana side): full policy with several rules
        policy("Acupuncture Services", "H-J", [
            section(OVERVIEW, "Overview"),
            section("Humana covers acupuncture for chronic low back pain. Humana covers up to 20 sessions per year. "
                    "Prior authorization is not required."),
            section("Providers must report modifier KX after session 12."),
        ]),
    ]


def uhc_policies():
    return [
        # Scenario H: different terminology ("CCM services" + abbreviation) for the same procedure as H-B
        policy("Care Coordination Services", "U-H", [
            section("UnitedHealthcare reimburses CCM services.", "Overview"),
            section("Chronic care management (CCM) services are reimbursed when a care plan is documented. "
                    "CCM services are limited to 1 per calendar month."),
        ], pdf="U-H.pdf"),
        # Scenario I (UHC side): same code 00031, diagnostic purpose
        policy("Laboratory – Diabetes Diagnostic Testing", "U-I", [
            section(OVERVIEW, "Overview"),
            section("Diagnostic testing with CPT code 00031 is reimbursable once every 90 days for diagnosis of diabetes."),
        ], pdf="U-I.pdf"),
        # Scenario J (UHC side): incomplete extraction (one truncated section, no payment policy section)
        policy("Acupuncture", "U-J", [
            section("UnitedHealthcare covers acupuncture for chronic low back pain under the medical benefit and "
                    "requires that providers who render acupuncture services meet all licensure requirements and",
                    "Overview"),
        ], pdf="U-J.pdf"),
    ]


@pytest.fixture(scope="session", autouse=True)
def synthetic_fixture():
    if not (FIXTURE_META / "MRCONSO.RRF").exists():
        import runpy
        runpy.run_path(str(ROOT / "tests" / "fixtures" / "make_fixture.py"), run_name="__main__")


@pytest.fixture()
def kb_con(tmp_path):
    con = connect(tmp_path / "test.duckdb")
    initialize_schema(con)
    with Run(con, "kb_build", umls_release="2026AA") as run:
        KnowledgeBaseBuilder(con, FIXTURE_META, "2026AA", run.run_id, with_attributes=True).build()
    yield con
    con.close()


@pytest.fixture()
def humana_file(tmp_path):
    p = tmp_path / "humana.json"
    p.write_text(json.dumps(humana_policies()), encoding="utf-8")
    return p


@pytest.fixture()
def uhc_file(tmp_path):
    p = tmp_path / "uhc.json"
    p.write_text(json.dumps(uhc_policies()), encoding="utf-8")
    return p


def run_map(con, path, payer=None, profile=None):
    from src.mapping.pipeline import run_mapping

    with Run(con, "map", input_file=str(path)) as run:
        run.stats = run_mapping(con, path, run, payer=payer, profile=profile)
    return run


def uhc_like(records):
    """Build records in the guidelines-document shape (as delivered by the UHC extraction)."""
    out = []
    for r in records:
        out.append({"policy_title": r.get("title"), "policy_number": r.get("number"), "effective_date": "1/1/2026",
                    "expiry_date": None, "Line_of_Business": r.get("lob", "Medicare Advantage"), "Billingclass": "Both",
                    "CPT_CODES_USED": r.get("codes", []),
                    "reimbursement_guidelines": {"title": "Reimbursement Guidelines", "content": r.get("content", ""),
                                                 "subsections": r.get("subsections", []), "tables": r.get("tables", [])},
                    "Question_Answer_Rules": r.get("qa"), "linked_pdf_contents": r.get("attachments"), "resources": None})
    return out


def mappings(con, number):
    return {r[0]: dict(zip(["code", "origin", "tier", "best_method", "sources", "ambiguous", "conflict", "flags",
                            "scope", "section_seqs", "review_status"], r))
            for r in con.execute("""SELECT m.code, m.mapping_origin, m.confidence_tier, m.best_method, m.evidence_sources,
                                           m.is_ambiguous, m.has_conflict, m.flags, m.scope, m.section_seqs, m.review_status
                                    FROM policy_cpt_mappings m JOIN policies p USING (policy_uid)
                                    WHERE p.policy_number = ?""", [number]).fetchall()}
