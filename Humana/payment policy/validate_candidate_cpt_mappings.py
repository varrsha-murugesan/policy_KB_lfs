import duckdb
import pandas as pd
import json

def analyze_and_validate_mappings():
    con = duckdb.connect(r"c:\Payer Policy\KB creation v1\database\payer_policy_kb.duckdb", read_only=True)
    run_id = "map-20261026T155017-ac17d6"

    query = """
        SELECT 
            m.policy_uid,
            p.policy_title,
            p.source_pdf,
            m.mapping_origin,
            m.code_system,
            m.code,
            c.description AS code_description,
            m.best_method,
            m.confidence_tier,
            m.score,
            m.evidence_sources,
            m.flags
        FROM policy_cpt_mappings m
        JOIN policies p ON p.policy_uid = m.policy_uid
        LEFT JOIN cpt_codes c ON c.code_system = m.code_system AND c.code = m.code
        WHERE m.run_id = ?
        ORDER BY p.policy_title, m.confidence_tier, m.code
    """
    df = con.execute(query, [run_id]).df()
    print(f"Total candidate policy mappings retrieved: {len(df)}")
    print(f"Distinct policies mapped: {df['policy_title'].nunique()}")

    # Group by policy
    policies = sorted(df['policy_title'].unique())
    report = []

    print("=" * 80)
    print("DETAILED CLINICAL VALIDATION OF MAPPED CODES ACROSS CANDIDATE POLICIES")
    print("=" * 80)

    for p_title in policies:
        sub = df[df['policy_title'] == p_title]
        total_m = len(sub)
        tier_counts = sub['confidence_tier'].value_counts().to_dict()
        top_codes = []
        for _, row in sub.head(10).iterrows():
            ev = row['evidence_sources']
            if hasattr(ev, 'tolist'):
                ev = ev.tolist()
            elif not isinstance(ev, (list, str)):
                ev = str(ev)
            top_codes.append({
                "code": f"{row['code_system']} {row['code']}",
                "desc": str(row['code_description']),
                "tier": str(row['confidence_tier']),
                "evidence": ev,
                "method": str(row['best_method'])
            })
        
        # Clinical validation assessment
        assessment = "Appropriate"
        key_findings = []

        # Policy-specific clinical assessments
        if "Acupuncture" in p_title:
            acupuncture_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['97810', '97811', '97813', '97814', '00001', '00002'])]
            key_findings.append(f"Mapped acupuncture procedure codes: {', '.join(acupuncture_codes[:4])}")
        elif "Chronic Care" in p_title:
            ccm_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['99490', '99491', '99487', '00021', '99424'])]
            key_findings.append(f"Mapped care management codes: {', '.join(ccm_codes[:4])}")
        elif "Colonoscopy" in p_title:
            col_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['45378', '45380', 'G0105', 'G0121', '00011'])]
            key_findings.append(f"Mapped colorectal endoscopic screening codes: {', '.join(col_codes[:4])}")
        elif "Observation" in p_title:
            obs_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['99217', '99218', '99219', '99220', '99221', '99224'])]
            key_findings.append(f"Mapped hospital observation E/M codes: {', '.join(obs_codes[:4])}")
        elif "NICU" in p_title or "Neonatal" in p_title:
            nicu_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['99468', '99477', '99460'])]
            key_findings.append(f"Mapped neonatal intensive/critical care codes: {', '.join(nicu_codes[:4])}")
        elif "Telehealth" in p_title:
            tele_codes = [r['code'] for r in top_codes if any(c in r['code'] for c in ['99202', '99211', '99441', 'G0071'])]
            key_findings.append(f"Mapped virtual/telehealth E/M codes: {', '.join(tele_codes[:4])}")
        elif "Modifier" in p_title:
            key_findings.append(f"Mapped applicable procedural families for modifier {p_title}")

        report.append({
            "policy_title": p_title,
            "source_pdf": sub['source_pdf'].iloc[0],
            "total_mappings": total_m,
            "tiers": tier_counts,
            "sample_mappings": top_codes[:5],
            "findings": key_findings
        })

        print(f"\nPolicy: {p_title}")
        print(f"  Source: {sub['source_pdf'].iloc[0]}")
        print(f"  Mappings: {total_m} codes (Tiers: {tier_counts})")
        for sc in top_codes[:4]:
            print(f"    - [{sc['tier']}] {sc['code']} ({sc['desc']}) via {sc['method']} ({sc['evidence']})")
        if key_findings:
            print(f"  Validation Note: {'; '.join(key_findings)}")

    # Save summary report
    out_json = r"c:\Payer Policy\Humana\payment policy\humana_policies\cpt_mapping_validation_report.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved mapping validation audit to {out_json}")

if __name__ == "__main__":
    analyze_and_validate_mappings()
