"""Generate a tiny SYNTHETIC UMLS release in tests/fixtures/META.

Nothing here is real CPT/HCPCS/SNOMED content:
* codes live in 00001-00099 (not assigned in CPT Category I; anesthesia starts at 00100)
  and A0001-A0003 used ONLY as synthetic HCPCS-format examples;
* CUIs use the C9900xxx range; AUIs/RUIs are fabricated test identifiers;
* every string was written for this test fixture.
It exists so the loader, matcher and comparison logic can be tested offline.
Run:  python tests/fixtures/make_fixture.py
"""
from pathlib import Path

META = Path(__file__).parent / "META"

# (CUI, SAB, TTY, CODE, STR, SRL, SUPPRESS, TS, STT, ISPREF)
ATOMS = [
    # --- acupuncture: two CPT codes + hierarchy heading -------------------
    ("C9900001", "CPT", "PT", "00001", "Acupuncture, needle insertion; first 15 minutes [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900002", "CPT", "PT", "00002", "Acupuncture, needle insertion; each additional 15 minutes [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900003", "MTHCH", "HT", "MTHU001", "Acupuncture Procedures", 3, "N", "P", "PF", "Y"),
    ("C9900060", "SNOMEDCT_US", "PT", "SCT-T-60", "Dry needling", 9, "N", "P", "PF", "Y"),
    # --- colonoscopy -------------------------------------------------------
    ("C9900011", "HCPCS", "PT", "A0001", "Colorectal cancer screening; colonoscopy, average risk [SYNTHETIC]", 0, "N", "P", "PF", "Y"),
    ("C9900011", "SNOMEDCT_US", "PT", "SCT-T-11", "Screening colonoscopy", 9, "N", "S", "PF", "Y"),
    ("C9900012", "CPT", "PT", "00011", "Colonoscopy, flexible; diagnostic [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900013", "CPT", "PT", "00012", "Colonoscopy, flexible; with biopsy [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    # --- chronic care management --------------------------------------------
    ("C9900021", "CPT", "PT", "00021", "Chronic care management services, first 20 minutes per month [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900021", "CPT", "SY", "00021", "Chronic care management", 3, "N", "S", "SY", "Y"),
    # --- hemoglobin A1c --------------------------------------------------------
    ("C9900031", "CPT", "PT", "00031", "Hemoglobin; glycosylated (A1C) [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900031", "CPT", "ETCLIN", "00031", "Hemoglobin A1c", 3, "N", "S", "VO", "Y"),
    ("C9900031", "MSH", "MH", "D-T-31", "Glycated hemoglobin test", 0, "N", "S", "PF", "Y"),
    # --- ambiguous head "ablation" ------------------------------------------------
    ("C9900041", "CPT", "PT", "00041", "Ablation, renal tissue, percutaneous [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900042", "CPT", "PT", "00042", "Ablation, cardiac tissue, catheter [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    # --- physical therapy evaluation ----------------------------------------------
    ("C9900051", "CPT", "PT", "00051", "Physical therapy evaluation, low complexity [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900051", "CPT", "SY", "00051", "Physical therapy evaluation", 3, "N", "S", "SY", "Y"),
    # --- recognized procedure without code link -------------------------------------
    ("C9900070", "SNOMEDCT_US", "PT", "SCT-T-70", "Hospital observation care", 9, "N", "P", "PF", "Y"),
    # --- explicit-code validation targets --------------------------------------------
    ("C9900099", "CPT", "PT", "00099", "Synthetic validation code [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900081", "HCPCS", "PT", "A0002", "Ground transport, basic life support [SYNTHETIC]", 0, "N", "P", "PF", "Y"),
    ("C9900082", "HCPCS", "PT", "A0003", "Ground transport, advanced life support [SYNTHETIC]", 0, "N", "P", "PF", "Y"),
    ("C9900091", "CPT", "PT", "00091", "Chiropractic manipulative treatment, one to two regions [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900092", "CPT", "PT", "00092", "Chiropractic manipulative treatment, three to four regions [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    ("C9900093", "CPT", "PT", "00093", "Chiropractic manipulative treatment, five regions [SYNTHETIC]", 3, "N", "P", "PF", "Y"),
    # an obsolete atom that must be ignored
    ("C9900094", "CPT", "PT", "00094", "Obsolete synthetic code", 3, "O", "P", "PF", "Y"),
]

STY = {"C9900001": ("T061", "Therapeutic or Preventive Procedure"), "C9900002": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900003": ("T061", "Therapeutic or Preventive Procedure"), "C9900060": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900011": ("T060", "Diagnostic Procedure"), "C9900012": ("T060", "Diagnostic Procedure"),
       "C9900013": ("T060", "Diagnostic Procedure"), "C9900021": ("T058", "Health Care Activity"),
       "C9900031": ("T059", "Laboratory Procedure"), "C9900041": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900042": ("T061", "Therapeutic or Preventive Procedure"), "C9900051": ("T058", "Health Care Activity"),
       "C9900070": ("T058", "Health Care Activity"), "C9900099": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900081": ("T061", "Therapeutic or Preventive Procedure"), "C9900082": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900091": ("T061", "Therapeutic or Preventive Procedure"), "C9900092": ("T061", "Therapeutic or Preventive Procedure"),
       "C9900093": ("T061", "Therapeutic or Preventive Procedure"), "C9900094": ("T061", "Therapeutic or Preventive Procedure")}

# (CUI1, REL, CUI2, RELA, SAB)   MRREL: CUI2 has REL to CUI1
RELS = [
    ("C9900003", "CHD", "C9900001", "", "MTHCH"),
    ("C9900003", "CHD", "C9900002", "", "MTHCH"),
    ("C9900001", "PAR", "C9900003", "", "MTHCH"),
    ("C9900002", "PAR", "C9900003", "", "MTHCH"),
    ("C9900060", "RQ", "C9900001", "", "SNOMEDCT_US"),   # 'dry needling' possibly synonymous -> related candidate
    ("C9900001", "RO", "C9900002", "add_on_code_for", "CPT"),  # stored, NOT used for inference
]

SABS = [("CPT", "CPT2026", "Current Procedural Terminology [fixture]", 3),
        ("HCPCS", "HCPCS2026", "HCPCS [fixture]", 0),
        ("HCPT", "HCPT2026", "CPT in HCPCS [fixture]", 3),
        ("MTHCH", "MTHCH2026", "CPT hierarchical terms [fixture]", 3),
        ("SNOMEDCT_US", "SNOMEDCT_US_2026", "SNOMED CT US [fixture]", 9),
        ("MSH", "MSH2026", "MeSH [fixture]", 0)]


def write(name, rows):
    META.mkdir(parents=True, exist_ok=True)
    with open(META / f"{name}.RRF", "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write("|".join("" if v is None else str(v) for v in r) + "|\n")


def main():
    conso = []
    for i, (cui, sab, tty, code, s, srl, sup, ts, stt, ispref) in enumerate(ATOMS, 1):
        conso.append([cui, "ENG", ts, f"L99{i:05d}", stt, f"S99{i:05d}", ispref, f"A99{i:05d}", "", code, "",
                      sab, tty, code, s, srl, sup, ""])
    write("MRCONSO", conso)
    write("MRSTY", [[cui, tui, "A1.2", sty, f"AT99{i:04d}", ""] for i, (cui, (tui, sty)) in enumerate(STY.items(), 1)])
    write("MRREL", [[c1, "", "CUI", rel, c2, "", "CUI", rela, f"R99{i:05d}", "", sab, sab, "", "Y", "N", ""]
                    for i, (c1, rel, c2, rela, sab) in enumerate(RELS, 1)])
    write("MRSAB", [["C99SAB", "C99SAB", vsab, rsab, son, rsab, "2026", "", "", "2026AA", "", "", "", srl, "", "", "",
                     "", "", "ENG", "UTF-8", "Y", "Y", son, ""] for rsab, vsab, son, srl in SABS])
    write("MRDEF", [["C9900031", "A99000013", "AT99D001", "", "MSH",
                     "A synthetic definition: laboratory measurement of glycated hemoglobin.", "N", ""]])
    write("MRRANK", [["0400", "CPT", "PT", "N"], ["0399", "HCPCS", "PT", "N"], ["0300", "SNOMEDCT_US", "PT", "N"],
                     ["0200", "MSH", "MH", "N"], ["0100", "CPT", "SY", "N"]])
    write("MRSAT", [["C9900012", "", "", "A99000007", "CODE", "00011", "AT99S001", "", "CPF", "CPT",
                     "Colonoscopy, flexible, proximal to splenic flexure; diagnostic [SYNTHETIC FULL]", "N", ""]])
    (META / "release.dat").write_text("umls.release.name=2026AA\n# SYNTHETIC TEST FIXTURE\n")
    print(f"Synthetic fixture written to {META}")


if __name__ == "__main__":
    main()
