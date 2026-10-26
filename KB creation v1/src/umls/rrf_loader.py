"""Build the terminology knowledge base from UMLS Metathesaurus RRF files.

Design
------
* DuckDB reads the pipe-delimited RRF files directly (no intermediate DBMS).
  RRF has no quoting, so quote/escape are disabled; a trailing '|' produces an
  empty FILLER column.
* Scope: atoms of the code vocabularies (CPT, HCPT, HCPCS) and their hierarchy
  sources (MTHCH, MTHHH), plus English atoms of every concept that is either
  code-bearing or has a procedure semantic type (procedure lexicon). This is
  what lets a SNOMED CT / MeSH / NCI synonym on the SAME CUI as a CPT code act
  as evidence, while the CUI link (not string similarity) asserts synonymy.
* Steps are idempotent and resumable: each runs in one transaction and is
  recorded in kb_build_steps. A failed step rolls back completely; re-running
  the build skips 'done' steps and redoes the failed one.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import duckdb

from config.settings import SETTINGS
from src.knowledge_base.db_manager import insert_records, transaction
from src.utils.text_normalizer import bag_key, descriptor_head, normalize

log = logging.getLogger(__name__)

RRF_COLUMNS = {
    "MRCONSO": ["CUI", "LAT", "TS", "LUI", "STT", "SUI", "ISPREF", "AUI", "SAUI", "SCUI", "SDUI", "SAB", "TTY",
                "CODE", "STR", "SRL", "SUPPRESS", "CVF"],
    "MRSTY": ["CUI", "TUI", "STN", "STY", "ATUI", "CVF"],
    "MRREL": ["CUI1", "AUI1", "STYPE1", "REL", "CUI2", "AUI2", "STYPE2", "RELA", "RUI", "SRUI", "SAB", "SL", "RG",
              "DIR", "SUPPRESS", "CVF"],
    "MRSAB": ["VCUI", "RCUI", "VSAB", "RSAB", "SON", "SF", "SVER", "VSTART", "VEND", "IMETA", "RMETA", "SLC", "SCC",
              "SRL", "TFR", "CFR", "CXTY", "TTYL", "ATNL", "LAT", "CENC", "CURVER", "SABIN", "SSN", "SCIT"],
    "MRDEF": ["CUI", "AUI", "ATUI", "SATUI", "SAB", "DEF", "SUPPRESS", "CVF"],
    "MRRANK": ["RANK", "SAB", "TTY", "SUPPRESS"],
    "MRSAT": ["CUI", "LUI", "SUI", "METAUI", "STYPE", "CODE", "ATUI", "SATUI", "ATN", "SAB", "ATV", "SUPPRESS", "CVF"],
}

CODE_PATTERNS = [  # (regex, code_system, category)
    (re.compile(r"^\d{5}$"), "CPT", "CPT_I"),
    (re.compile(r"^\d{4}F$"), "CPT", "CPT_II"),
    (re.compile(r"^\d{4}T$"), "CPT", "CPT_III"),
    (re.compile(r"^\d{4}U$"), "CPT", "CPT_PLA"),
    (re.compile(r"^\d{4}M$"), "CPT", "CPT_MAAA"),
    (re.compile(r"^[A-V]\d{4}$"), "HCPCS_L2", "HCPCS_L2"),
]

STEPS = ["source_vocabularies", "stage_atoms", "concepts", "terms", "semantic_types", "definitions",
         "cpt_codes", "relationships", "attributes", "code_enrichment", "finalize"]


class MissingSourceFileError(FileNotFoundError):
    pass


class ReleaseMismatchError(RuntimeError):
    pass


def classify_code(code: str) -> tuple[str, str] | None:
    for rx, system, cat in CODE_PATTERNS:
        if rx.match(code or ""):
            return system, cat
    return None


def find_rrf(meta_dir: Path, name: str, required: bool = True) -> list[Path]:
    files = sorted(p for p in meta_dir.glob(f"{name}.RRF*") if not p.name.endswith(".part"))
    if not files and required:
        raise MissingSourceFileError(
            f"{name}.RRF not found in {meta_dir}. Run scripts/download_umls_data.py or copy the RRF files "
            f"from the UMLS release META folder into {meta_dir}."
        )
    return files


def rrf_relation(meta_dir: Path, name: str) -> str:
    files = find_rrf(meta_dir, name)
    cols = RRF_COLUMNS[name] + ["FILLER"]
    spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in cols) + "}"
    paths = "[" + ", ".join("'" + str(p).replace("\\", "/").replace("'", "''") + "'" for p in files) + "]"
    return (f"read_csv({paths}, delim='|', quote='', escape='', header=false, columns={spec}, "
            f"auto_detect=false, null_padding=true, parallel=true)")


def detect_release(meta_dir: Path) -> str | None:
    for candidate in [meta_dir / "release.dat", meta_dir.parent / "release.dat"]:
        if candidate.exists():
            m = re.search(r"\b(20\d{2}A[AB])\b", candidate.read_text(errors="ignore"))
            if m:
                return m.group(1)
    for part in [meta_dir.resolve(), *meta_dir.resolve().parents][:4]:
        m = re.search(r"(20\d{2}A[AB])", part.name)
        if m:
            return m.group(1)
    return None


def _files_signature(meta_dir: Path, names: list[str]) -> str:
    sig = []
    for n in names:
        for p in find_rrf(meta_dir, n, required=False):
            st = p.stat()
            sig.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
    return ";".join(sig)


class KnowledgeBaseBuilder:
    def __init__(self, con: duckdb.DuckDBPyConnection, meta_dir: Path, release: str, run_id: str,
                 force_steps: set[str] | None = None, with_attributes: bool = False):
        self.con, self.meta_dir, self.release, self.run_id = con, Path(meta_dir), release, run_id
        self.force = force_steps or set()
        self.with_attributes = with_attributes
        v = SETTINGS.vocab
        self.code_sabs = list(v.code_sabs)
        self.target_sabs = list(v.code_sabs) + list(v.hierarchy_sabs)
        self.stats: dict = {}

    # ------------------------------------------------------------ control --
    def check_release(self, replace: bool) -> None:
        active = self.con.execute("SELECT value FROM schema_meta WHERE key='active_umls_release'").fetchone()
        loaded = self.con.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        if active and active[0] != self.release and loaded and not replace:
            raise ReleaseMismatchError(
                f"The knowledge base holds UMLS {active[0]}; you are loading {self.release}. "
                "Re-run with --replace to rebuild for the new release (existing mappings keep their release tag)."
            )
        if active and active[0] != self.release and replace:
            log.warning("Replacing KB release %s with %s", active[0], self.release)
            self.con.execute("DELETE FROM kb_build_steps WHERE umls_release=?", [self.release])
            self.force |= set(STEPS)

    def step_done(self, step: str) -> bool:
        row = self.con.execute("SELECT status FROM kb_build_steps WHERE umls_release=? AND step=?",
                               [self.release, step]).fetchone()
        return bool(row and row[0] == "done") and step not in self.force

    def _mark(self, step: str, status: str, rows: int | None = None, detail: str | None = None, files: list | None = None):
        now = datetime.now()
        self.con.execute(
            "INSERT INTO kb_build_steps (umls_release, step, status, row_count, source_files, run_id, started_at, finished_at, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (umls_release, step) DO UPDATE SET status=excluded.status,"
            " row_count=excluded.row_count, source_files=excluded.source_files, run_id=excluded.run_id,"
            " started_at=CASE WHEN excluded.status='running' THEN excluded.started_at ELSE kb_build_steps.started_at END,"
            " finished_at=excluded.finished_at, detail=excluded.detail",
            [self.release, step, status, rows, _files_signature(self.meta_dir, files or []) if files else None,
             self.run_id, now, None if status == "running" else now, detail],
        )

    def build(self) -> dict:
        # Fail fast on missing required files before touching the database.
        for name in SETTINGS.umls.required_rrf:
            find_rrf(self.meta_dir, name)
        # Steps between stage_atoms and finalize read the staging table, which
        # finalize drops. If any of them must run and staging is gone, re-stage.
        staged = self.con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='stg_atoms'").fetchone()[0]
        needs_stage = STEPS[STEPS.index("stage_atoms") + 1: STEPS.index("cpt_codes") + 1]
        if not staged and any(not self.step_done(s) for s in needs_stage):
            self.force.add("stage_atoms")
        for i, step in enumerate(STEPS):
            if self.step_done(step):
                log.info("[%s] already done for %s - skipping", step, self.release)
                continue
            # A redone step invalidates everything downstream of it.
            for later in STEPS[i + 1:]:
                self.force.add(later)
            self._mark(step, "running")
            log.info("[%s] running", step)
            try:
                with transaction(self.con):
                    rows, files = getattr(self, f"_step_{step}")()
                self._mark(step, "done", rows, files=files)
                self.stats[step] = rows
                log.info("[%s] done: %s rows", step, rows)
            except Exception as exc:
                self._mark(step, "failed", detail=f"{type(exc).__name__}: {exc}"[:2000])
                log.error("[%s] failed - transaction rolled back. Fix the cause and re-run to resume. %s", step, exc)
                raise
        return self.stats

    # -------------------------------------------------------------- steps --
    def _step_source_vocabularies(self):
        self.con.execute("DELETE FROM source_vocabularies")
        code_list = ", ".join(f"'{s}'" for s in self.code_sabs)
        self.con.execute(f"""
            INSERT INTO source_vocabularies (rsab, vsab, son, srl, sver, lat, is_code_source, umls_release)
            SELECT RSAB, any_value(VSAB), any_value(SON), TRY_CAST(any_value(SRL) AS INTEGER), any_value(SVER),
                   any_value(LAT), RSAB IN ({code_list}), ?
            FROM {rrf_relation(self.meta_dir, 'MRSAB')} WHERE CURVER = 'Y' GROUP BY RSAB""", [self.release])
        n = self.con.execute("SELECT COUNT(*) FROM source_vocabularies").fetchone()[0]
        present = {r[0] for r in self.con.execute("SELECT rsab FROM source_vocabularies").fetchall()}
        missing = [s for s in self.code_sabs if s not in present]
        if missing:
            log.warning("Code vocabularies absent from this release/subset: %s. Codes from them cannot be validated. "
                        "If you used a Level-0 subset, CPT (restriction category 3) is excluded by design.", missing)
        self.stats["missing_code_sabs"] = missing
        return n, ["MRSAB"]

    def _step_stage_atoms(self):
        v = SETTINGS.vocab
        targets = ", ".join(f"'{s}'" for s in self.target_sabs)
        tuis = ", ".join(f"'{t}'" for t in v.procedure_tuis)
        self.con.execute("DROP TABLE IF EXISTS stg_atoms")
        self.con.execute(f"""
            CREATE TABLE stg_atoms AS
            WITH conso AS (SELECT * FROM {rrf_relation(self.meta_dir, 'MRCONSO')}),
            code_cuis AS (SELECT DISTINCT CUI FROM conso WHERE SAB IN ({targets})),
            proc_cuis AS (SELECT DISTINCT CUI FROM {rrf_relation(self.meta_dir, 'MRSTY')} WHERE TUI IN ({tuis}))
            SELECT c.CUI, c.LAT, c.TS, c.STT, c.ISPREF, c.AUI, c.SCUI, c.SAB, c.TTY, c.CODE, c.STR,
                   TRY_CAST(c.SRL AS INTEGER) AS SRL, c.SUPPRESS,
                   (c.CUI IN (SELECT CUI FROM code_cuis)) AS code_bearing,
                   (c.CUI IN (SELECT CUI FROM proc_cuis)) AS in_proc
            FROM conso c
            WHERE c.SUPPRESS <> 'O'
              AND (c.SAB IN ({targets})
                   OR (c.LAT = 'ENG'
                       AND TRY_CAST(c.SRL AS INTEGER) <= {int(v.max_lexicon_srl)}
                       AND (c.CUI IN (SELECT CUI FROM code_cuis) OR c.CUI IN (SELECT CUI FROM proc_cuis))))
        """)
        return self.con.execute("SELECT COUNT(*) FROM stg_atoms").fetchone()[0], ["MRCONSO", "MRSTY"]

    def _clear_concept_dependents(self):
        for t in ("terms", "concept_semantic_types", "concept_definitions", "cpt_codes"):
            self.con.execute(f"DELETE FROM {t}")

    def _step_concepts(self):
        self._clear_concept_dependents()
        self.con.execute("DELETE FROM concepts")
        rank = ""
        if find_rrf(self.meta_dir, "MRRANK", required=False):
            self.con.execute(f"CREATE OR REPLACE TEMP TABLE tmp_rank AS SELECT SAB, TTY, TRY_CAST(RANK AS INTEGER) AS r "
                             f"FROM {rrf_relation(self.meta_dir, 'MRRANK')}")
            rank = "LEFT JOIN tmp_rank rk ON rk.SAB = a.SAB AND rk.TTY = a.TTY"
        order = "rk.r DESC NULLS LAST," if rank else ""
        self.con.execute(f"""
            INSERT INTO concepts (cui, preferred_name, preferred_sab, is_code_bearing, in_procedure_lexicon, umls_release)
            SELECT CUI, arg_min(STR, k), arg_min(SAB, k), bool_or(code_bearing), bool_or(in_proc), ?
            FROM (
              SELECT a.*, row_number() OVER (PARTITION BY a.CUI ORDER BY
                     (a.LAT='ENG') DESC, (a.TS='P') DESC, (a.STT='PF') DESC, (a.ISPREF='Y') DESC, {order} a.AUI) AS k
              FROM stg_atoms a {rank}
            ) GROUP BY CUI""", [self.release])
        return self.con.execute("SELECT COUNT(*) FROM concepts").fetchone()[0], ["MRCONSO", "MRRANK"]

    def _step_terms(self):
        self.con.execute("DELETE FROM terms")
        code_sabs = set(self.code_sabs)
        code_ttys = set(SETTINGS.vocab.code_level_ttys)
        total = 0
        cur = self.con.cursor()
        cur.execute("SELECT CUI, LAT, TS, STT, ISPREF, AUI, SCUI, SAB, TTY, CODE, STR, SRL, SUPPRESS FROM stg_atoms")
        while True:
            rows = cur.fetchmany(200_000)
            if not rows:
                break
            recs = []
            for (cui, lat, ts, stt, ispref, aui, scui, sab, tty, code, s, srl, sup) in rows:
                is_code_level = (sab in code_sabs and tty in code_ttys and classify_code(code) is not None)
                recs.append({
                    "aui": aui, "cui": cui, "sab": sab, "code": code, "scui": scui, "tty": tty,
                    "str": s, "str_norm": normalize(s), "str_key": bag_key(s),
                    "head_norm": descriptor_head(s) if is_code_level else None,
                    "lat": lat, "ispref": ispref, "ts": ts, "stt": stt, "suppress": sup,
                    "srl": srl, "is_code_level": is_code_level, "umls_release": self.release,
                })
            total += insert_records(self.con, "terms", recs)
            log.info("  terms inserted: %d", total)
        cur.close()
        return total, ["MRCONSO"]

    def _step_semantic_types(self):
        self.con.execute("DELETE FROM concept_semantic_types")
        self.con.execute(f"""
            INSERT INTO concept_semantic_types (cui, tui, sty)
            SELECT DISTINCT s.CUI, s.TUI, s.STY FROM {rrf_relation(self.meta_dir, 'MRSTY')} s
            JOIN concepts c ON c.cui = s.CUI""")
        return self.con.execute("SELECT COUNT(*) FROM concept_semantic_types").fetchone()[0], ["MRSTY"]

    def _step_definitions(self):
        self.con.execute("DELETE FROM concept_definitions")
        if not find_rrf(self.meta_dir, "MRDEF", required=False):
            log.warning("MRDEF.RRF not present - definitions skipped")
            return 0, []
        self.con.execute(f"""
            INSERT INTO concept_definitions (atui, cui, sab, definition, suppress)
            SELECT d.ATUI, d.CUI, d.SAB, d.DEF, d.SUPPRESS FROM {rrf_relation(self.meta_dir, 'MRDEF')} d
            JOIN concepts c ON c.cui = d.CUI
            LEFT JOIN source_vocabularies v ON v.rsab = d.SAB
            WHERE COALESCE(v.srl, 0) <= {int(SETTINGS.vocab.max_lexicon_srl)} AND d.SUPPRESS <> 'O'""")
        return self.con.execute("SELECT COUNT(*) FROM concept_definitions").fetchone()[0], ["MRDEF"]

    def _step_cpt_codes(self):
        self.con.execute("DELETE FROM cpt_codes")
        rows = self.con.execute("""
            SELECT sab, code, cui, tty, str, suppress FROM terms WHERE is_code_level
            ORDER BY sab, code,
                     CASE tty WHEN 'PT' THEN 0 WHEN 'ETCLIN' THEN 1 WHEN 'SY' THEN 2 WHEN 'POS' THEN 3 ELSE 9 END,
                     (suppress = 'N') DESC, aui""").fetchall()
        recs, seen = [], set()
        for sab, code, cui, tty, s, sup in rows:
            if (sab, code) in seen:
                continue
            seen.add((sab, code))
            system, cat = classify_code(code)
            recs.append({"sab": sab, "code": code, "code_system": system, "code_category": cat, "primary_cui": cui,
                         "description": s, "description_tty": tty, "full_description": None,
                         "is_suppressed": sup != "N", "umls_release": self.release})
        return insert_records(self.con, "cpt_codes", recs), ["MRCONSO"]

    def _step_relationships(self):
        self.con.execute("DELETE FROM concept_relationships")
        temp_dir = self.meta_dir.parent / ".duckdb_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.con.execute(f"PRAGMA temp_directory='{str(temp_dir).replace(chr(92), '/')}'")
        self.con.execute("PRAGMA max_memory='10GB'")
        # Focus on candidate relationship types used for terminology inference
        candidate_rels = tuple(set(SETTINGS.vocab.candidate_rels + ("PAR", "RB")))
        rels = ", ".join(f"'{r}'" for r in candidate_rels)
        self.con.execute(f"""
            INSERT INTO concept_relationships (rui, cui1, rel, cui2, rela, sab, dir, suppress, umls_release)
            SELECT r.RUI, r.CUI1, r.REL, r.CUI2, NULLIF(r.RELA, ''), r.SAB, NULLIF(r.DIR, ''), r.SUPPRESS, ?
            FROM {rrf_relation(self.meta_dir, 'MRREL')} r
            LEFT JOIN source_vocabularies v ON v.rsab = r.SAB
            WHERE r.REL IN ({rels}) AND r.CUI1 <> r.CUI2 AND r.SUPPRESS <> 'O'
              AND COALESCE(v.srl, 0) <= {int(SETTINGS.vocab.max_lexicon_srl)}
              AND r.CUI1 IN (SELECT cui FROM concepts) AND r.CUI2 IN (SELECT cui FROM concepts)
              AND (r.CUI1 IN (SELECT cui FROM concepts WHERE is_code_bearing) OR r.CUI2 IN (SELECT cui FROM concepts WHERE is_code_bearing))
            QUALIFY row_number() OVER (PARTITION BY r.RUI) = 1""", [self.release])
        return self.con.execute("SELECT COUNT(*) FROM concept_relationships").fetchone()[0], ["MRREL"]

    def _step_attributes(self):
        if not self.with_attributes:
            log.info("[attributes] skipped (use --with-attributes to load CPT full descriptions from MRSAT)")
            return 0, []
        if not find_rrf(self.meta_dir, "MRSAT", required=False):
            log.warning("MRSAT.RRF not present - CPT full descriptions (CPF) skipped")
            return 0, []
        sabs = ", ".join(f"'{s}'" for s in self.code_sabs)
        self.con.execute(f"""
            UPDATE cpt_codes SET full_description = s.ATV
            FROM (SELECT SAB, CODE, any_value(ATV) AS ATV FROM {rrf_relation(self.meta_dir, 'MRSAT')}
                  WHERE ATN = 'CPF' AND SAB IN ({sabs}) GROUP BY SAB, CODE) s
            WHERE cpt_codes.sab = s.SAB AND cpt_codes.code = s.CODE""")
        return self.con.execute("SELECT COUNT(*) FROM cpt_codes WHERE full_description IS NOT NULL").fetchone()[0], ["MRSAT"]

    def _step_code_enrichment(self):
        from src.enrichment.umls_enrichment import build_code_enrichment

        stats = build_code_enrichment(self.con)
        self.stats["code_enrichment"] = stats
        return stats["codes"], []

    def _step_finalize(self):
        self.con.execute("DROP TABLE IF EXISTS stg_atoms")
        self.con.execute("INSERT INTO schema_meta (key, value) VALUES ('active_umls_release', ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()", [self.release])
        return 1, []
