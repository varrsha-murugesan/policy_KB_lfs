"""UMLS enrichment beyond code lookup, built ONLY from what the loaded KB contains.

1. code_enrichment: code -> CUIs, semantic types (MRSTY), and a service family:
   the nearest parent concept in the hierarchy relationships actually loaded
   (MRREL REL='PAR' from the CPT/HCPCS hierarchy sources). If the release has no
   such rows for a code, family_source = 'none_available' - nothing is invented.
2. policy_service_groups: families covered by a policy's high-confidence codes.
3. Mapping-quality signals written as mapping flags:
   - inferred_family_differs_from_stated_codes: an inferred-only code whose family
     is not among the families of codes the policy states explicitly
   - non_procedure_semantic_type: inferred evidence whose concept has semantic
     types, none of them in the procedure/device/drug allow-list
   - policy_description_mismatch is produced earlier by the code extractor.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import duckdb

from config.settings import SETTINGS
from src.knowledge_base.db_manager import insert_records

log = logging.getLogger(__name__)
# Semantic types for which a CPT/HCPCS candidate is plausible (procedures, activities,
# devices, drugs/substances, laboratory/diagnostic). Used ONLY as a review signal.
CODEABLE_TUIS = {"T058", "T059", "T060", "T061", "T063", "T074", "T075", "T121", "T200", "T109", "T116", "T195",
                 "T125", "T126", "T127", "T129", "T130", "T131", "T197", "T203", "T065", "T062"}
SAB_PRIORITY = {"MTHCH": 0, "CPT": 1, "MTHHH": 2, "HCPCS": 3, "HCPT": 4}


def build_code_enrichment(con: duckdb.DuckDBPyConnection) -> dict:
    release = con.execute("SELECT value FROM schema_meta WHERE key='active_umls_release'").fetchone()
    release = release[0] if release else None
    codes = con.execute("""SELECT code_system, code, sab FROM cpt_codes
                           ORDER BY code_system, code, CASE sab WHEN 'CPT' THEN 0 WHEN 'HCPCS' THEN 1 ELSE 2 END""").fetchall()
    first = {}
    for system, code, sab in codes:
        first.setdefault((system, code), sab)
    cui_rows = con.execute("SELECT sab, code, cui FROM v_code_concepts ORDER BY 1, 2, 3").fetchall()
    cuis = defaultdict(list)
    for sab, code, cui in cui_rows:
        cuis[(sab, code)].append(cui)
    sty = defaultdict(set)
    for cui, tui, name in con.execute("SELECT cui, tui, sty FROM concept_semantic_types").fetchall():
        sty[cui].add((tui, name))
    hier_sabs = list(SETTINGS.vocab.hierarchy_sabs) + list(SETTINGS.vocab.code_sabs)
    parents = defaultdict(list)
    for c1, c2, sab in con.execute("""SELECT cui1, cui2, sab FROM concept_relationships
                                      WHERE rel = 'PAR' AND sab IN (SELECT unnest(?)) ORDER BY 1, 2, 3""", [hier_sabs]).fetchall():
        parents[c1].append((SAB_PRIORITY.get(sab, 9), c2, sab))
    names = dict(con.execute("SELECT cui, preferred_name FROM concepts").fetchall())
    recs = []
    for (system, code), sab in sorted(first.items()):
        cl = sorted(set(cuis.get((sab, code), [])))
        st = sorted({x for c in cl for x in sty.get(c, set())})
        par = sorted(p for c in cl for p in parents.get(c, []))
        fam = par[0] if par else None
        recs.append({"code_system": system, "code": code, "sab": sab, "cuis": cl, "tuis": [t for t, _ in st],
                     "semantic_types": [n for _, n in st], "family_cui": fam[1] if fam else None,
                     "family_name": names.get(fam[1]) if fam else None, "family_size": None,
                     "family_source": fam[2] if fam else "none_available", "umls_release": release})
    size = defaultdict(int)
    for r in recs:
        if r["family_cui"]:
            size[r["family_cui"]] += 1
    for r in recs:
        r["family_size"] = size.get(r["family_cui"]) if r["family_cui"] else None
    con.execute("DELETE FROM code_enrichment")
    insert_records(con, "code_enrichment", recs)
    stats = {"codes": len(recs), "with_family": sum(1 for r in recs if r["family_cui"]),
             "with_semantic_type": sum(1 for r in recs if r["tuis"]), "families": len(size)}
    log.info("code_enrichment: %s", stats)
    return stats


def ensure_code_enrichment(con) -> None:
    n_codes = con.execute("SELECT COUNT(*) FROM cpt_codes").fetchone()[0]
    n_enr = con.execute("SELECT COUNT(*) FROM code_enrichment").fetchone()[0]
    if n_codes and n_enr == 0:
        build_code_enrichment(con)


def apply_enrichment(con, policy_uids: list[str], run_id: str) -> dict:
    """Service groups + quality signals for the given policies (inside the caller's transaction)."""
    con.execute("DELETE FROM policy_service_groups WHERE policy_uid IN (SELECT unnest(?))", [policy_uids])
    rows = con.execute("""
        SELECT m.policy_uid, e.family_cui, any_value(e.family_name), COUNT(DISTINCT m.code),
               list(DISTINCT m.code_provenance ORDER BY m.code_provenance)
        FROM policy_cpt_mappings m JOIN code_enrichment e ON e.code_system = m.code_system AND e.code = m.code
        WHERE m.policy_uid IN (SELECT unnest(?)) AND e.family_cui IS NOT NULL AND m.review_status <> 'rejected'
          AND (m.confidence_tier IN ('A','B','C') OR m.review_status = 'approved')
        GROUP BY 1, 2 ORDER BY 1, 2""", [policy_uids]).fetchall()
    insert_records(con, "policy_service_groups", [
        {"policy_uid": u, "family_cui": f, "family_name": n, "n_codes": c, "provenance": p, "run_id": run_id}
        for u, f, n, c, p in rows])
    # signal 1: inferred-only code outside the families of stated codes
    n1 = con.execute("""
        WITH stated AS (SELECT DISTINCT m.policy_uid, e.family_cui FROM policy_cpt_mappings m
                        JOIN code_enrichment e ON e.code_system = m.code_system AND e.code = m.code
                        WHERE m.mapping_origin NOT IN ('inferred', 'reviewer_added') AND e.family_cui IS NOT NULL)
        UPDATE policy_cpt_mappings m SET flags = list_append(m.flags, 'inferred_family_differs_from_stated_codes'),
               needs_review = TRUE
        FROM code_enrichment e
        WHERE m.policy_uid IN (SELECT unnest(?)) AND m.mapping_origin = 'inferred'
          AND e.code_system = m.code_system AND e.code = m.code AND e.family_cui IS NOT NULL
          AND m.policy_uid IN (SELECT policy_uid FROM stated)
          AND NOT EXISTS (SELECT 1 FROM stated s WHERE s.policy_uid = m.policy_uid AND s.family_cui = e.family_cui)
        RETURNING 1""", [policy_uids]).fetchall()
    # semantic types onto evidence, then signal 2
    con.execute("""
        UPDATE mapping_evidence ev SET semantic_types = s.stys
        FROM (SELECT cui, list(DISTINCT tui ORDER BY tui) AS stys FROM concept_semantic_types GROUP BY cui) s
        WHERE ev.policy_uid IN (SELECT unnest(?)) AND ev.cui = s.cui""", [policy_uids])
    n2 = con.execute("""
        UPDATE policy_cpt_mappings m SET flags = list_append(m.flags, 'non_procedure_semantic_type'), needs_review = TRUE
        WHERE m.policy_uid IN (SELECT unnest(?)) AND m.mapping_origin = 'inferred'
          AND EXISTS (SELECT 1 FROM mapping_evidence ev WHERE ev.mapping_id = m.mapping_id AND ev.semantic_types IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM mapping_evidence ev WHERE ev.mapping_id = m.mapping_id
                          AND len(list_intersect(ev.semantic_types, ?)) > 0)
        RETURNING 1""", [policy_uids, sorted(CODEABLE_TUIS)]).fetchall()
    return {"service_groups": len(rows), "flag_inferred_family_differs": len(n1), "flag_non_procedure_sty": len(n2)}
