"""KB build from synthetic RRF files; resumability (Scenario K); release safety; API client & secrets."""
import json
import os

import pytest
import requests

from conftest import FIXTURE_META
from src.knowledge_base.db_manager import Run, connect, initialize_schema
from src.knowledge_base.validation import KB_CHECKS, run_checks
from src.umls import downloader
from src.umls.api_client import UmlsApiClient, UmlsAuthError
from src.umls.rrf_loader import (KnowledgeBaseBuilder, MissingSourceFileError, ReleaseMismatchError, classify_code,
                                 detect_release)


def test_kb_contents(kb_con):
    codes = dict(kb_con.execute("SELECT sab, COUNT(*) FROM cpt_codes GROUP BY 1").fetchall())
    assert codes == {"CPT": 13, "HCPCS": 3}
    assert kb_con.execute("SELECT COUNT(*) FROM terms WHERE code='00094'").fetchone()[0] == 0  # obsolete excluded
    # cross-vocabulary synonym kept on the same CUI as the HCPCS code
    assert kb_con.execute("SELECT sab FROM terms WHERE str='Screening colonoscopy'").fetchone()[0] == "SNOMEDCT_US"
    assert kb_con.execute("SELECT head_norm FROM terms WHERE code='00011' AND tty='PT'").fetchone()[0] == "colonoscopy"
    assert kb_con.execute("SELECT full_description FROM cpt_codes WHERE code='00011'").fetchone()[0].endswith("[SYNTHETIC FULL]")
    assert kb_con.execute("SELECT COUNT(*) FROM concept_definitions").fetchone()[0] == 1
    assert all(ok for _n, ok, _d in run_checks(kb_con, KB_CHECKS))


def test_classify_code():
    assert classify_code("45378") == ("CPT", "CPT_I")
    assert classify_code("0012M") == ("CPT", "CPT_MAAA")
    assert classify_code("0165U") == ("CPT", "CPT_PLA")
    assert classify_code("G0121") == ("HCPCS_L2", "HCPCS_L2")
    assert classify_code("W0001") is None and classify_code("MTHU001") is None


def test_detect_release():
    assert detect_release(FIXTURE_META) == "2026AA"


def test_interrupted_build_resumes_without_duplicates(tmp_path, monkeypatch):
    """Scenario K: a step fails mid-build -> rollback; re-run skips done steps and finishes."""
    con = connect(tmp_path / "k.duckdb")
    initialize_schema(con)
    original = KnowledgeBaseBuilder._step_relationships
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            con.execute("INSERT INTO concept_relationships SELECT 'PARTIAL', 'C1', 'RO', 'C2', NULL, 'X', NULL, 'N', '2026AA'")
            raise OSError("simulated disk/network failure")
        return original(self)

    monkeypatch.setattr(KnowledgeBaseBuilder, "_step_relationships", flaky)
    with pytest.raises(OSError):
        with Run(con, "kb_build", umls_release="2026AA") as run:
            KnowledgeBaseBuilder(con, FIXTURE_META, "2026AA", run.run_id).build()
    assert con.execute("SELECT status FROM kb_build_steps WHERE step='relationships'").fetchone()[0] == "failed"
    assert con.execute("SELECT COUNT(*) FROM concept_relationships WHERE rui='PARTIAL'").fetchone()[0] == 0  # rolled back
    n_terms = con.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
    with Run(con, "kb_build", umls_release="2026AA") as run:
        stats = KnowledgeBaseBuilder(con, FIXTURE_META, "2026AA", run.run_id).build()
    assert "terms" not in stats and "relationships" in stats       # done steps skipped
    assert con.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == n_terms
    assert con.execute("SELECT COUNT(*) FROM concept_relationships").fetchone()[0] == 6
    assert all(ok for _n, ok, _d in run_checks(con, KB_CHECKS))


def test_release_mismatch_requires_replace(kb_con):
    b = KnowledgeBaseBuilder(kb_con, FIXTURE_META, "2026AB", "r")
    with pytest.raises(ReleaseMismatchError):
        b.check_release(replace=False)


def test_missing_rrf_fails_loudly(tmp_path):
    con = connect(tmp_path / "m.duckdb")
    initialize_schema(con)
    with pytest.raises(MissingSourceFileError):
        KnowledgeBaseBuilder(con, tmp_path, "2026AA", "r").build()


class FakeResp:
    def __init__(self, status, body=b"", headers=None, payload=None):
        self.status_code, self._body, self.headers, self._payload = status, body, headers or {}, payload

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_api_client_retries_caches_and_hides_key(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("UMLS_API_KEY", "SECRET-KEY-123456")
    seen = []

    class S(requests.Session):
        def get(self, url, params=None, timeout=None, **kw):
            seen.append(dict(params))
            return FakeResp(429, headers={"Retry-After": "0"}) if len(seen) == 1 else FakeResp(200, payload={"result": {"ok": 1}})

    c = UmlsApiClient(cache_dir=tmp_path, session=S())
    assert c.get("/x", {"a": 1}) == {"result": {"ok": 1}}
    assert c.get("/x", {"a": 1}) == {"result": {"ok": 1}}         # served from cache
    assert len(seen) == 2 and seen[0]["apiKey"] == "SECRET-KEY-123456"
    assert not any("SECRET-KEY" in f.read_text() for f in tmp_path.iterdir())  # key never cached


def test_api_client_bad_key(tmp_path, monkeypatch):
    monkeypatch.setenv("UMLS_API_KEY", "bad")

    class S(requests.Session):
        def get(self, *a, **k):
            return FakeResp(401)

    with pytest.raises(UmlsAuthError):
        UmlsApiClient(cache_dir=tmp_path, session=S()).get("/content/current/CUI/C1", use_cache=False)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("UMLS_API_KEY", raising=False)
    with pytest.raises(UmlsAuthError):
        UmlsApiClient()


def test_download_resumes_with_range(tmp_path, monkeypatch):
    """Scenario K for downloads: a .part file is continued with an HTTP Range request."""
    monkeypatch.setenv("UMLS_API_KEY", "k" * 12)
    (tmp_path / "rel.zip.part").write_bytes(b"HELLO ")
    (tmp_path / "rel.zip.manifest.json").write_text(json.dumps({"size": 11}))
    ranges = []

    class S(requests.Session):
        def get(self, url, params=None, headers=None, **kw):
            ranges.append((headers or {}).get("Range"))
            return FakeResp(206, b"WORLD", {"Content-Range": "bytes 6-10/11", "Content-Type": "application/zip"})

    out = downloader.download_release("https://download.example/umls/rel.zip", tmp_path, session=S())
    assert out.read_bytes() == b"HELLO WORLD" and ranges == ["bytes=6-"]


def test_logging_redacts_key(monkeypatch, capsys):
    import logging

    from src.utils.logging_config import setup_logging
    monkeypatch.setenv("UMLS_API_KEY", "TOPSECRETKEY99")
    setup_logging("t")
    logging.getLogger("t").info("calling https://x/y?apiKey=TOPSECRETKEY99&z=1 key TOPSECRETKEY99")
    out = capsys.readouterr().out
    assert "TOPSECRETKEY99" not in out and "REDACTED" in out
