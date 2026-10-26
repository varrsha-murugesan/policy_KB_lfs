"""UMLS Terminology Services (UTS) REST API client.

Role in this project: credential verification, vocabulary-access checks and
on-demand spot lookups. The knowledge base itself is built from the downloaded
RRF files (see rrf_loader.py) because the API is rate limited (20 req/s/IP),
search results are capped per page, and a release-pinned local copy is
reproducible.

Authentication: every call needs `apiKey=<key>` (NLM UTS documentation). The
key is read from the UMLS_API_KEY environment variable (or .env) and is never
logged: URLs are logged without query strings and the logging filter redacts
apiKey=... defensively.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from config.settings import SETTINGS

log = logging.getLogger(__name__)


class UmlsAuthError(RuntimeError):
    """Missing, invalid or expired API key."""


class UmlsApiError(RuntimeError):
    pass


def get_api_key(required: bool = True) -> str | None:
    key = (os.environ.get("UMLS_API_KEY") or "").strip()
    if not key and required:
        raise UmlsAuthError(
            "UMLS_API_KEY is not set. Copy .env.example to .env and put your key there "
            "(the .env file is excluded from git)."
        )
    return key or None


class _RateLimiter:
    def __init__(self, rps: float):
        self.min_interval = 1.0 / max(rps, 0.1)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


@dataclass
class AccessReport:
    authenticated: bool
    vocabularies: dict  # sab -> {"accessible": bool, "results": int, "note": str}
    release: str | None

    def ok_for(self, sab: str) -> bool:
        return bool(self.vocabularies.get(sab, {}).get("accessible"))


class UmlsApiClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None, session: requests.Session | None = None):
        self._api_key = api_key or get_api_key()
        self.base = SETTINGS.umls.rest_base
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "payer-policy-intelligence/1.0"})
        self.limiter = _RateLimiter(SETTINGS.umls.max_requests_per_second)
        self.cache_dir = cache_dir or SETTINGS.paths.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = SETTINGS.umls.cache_ttl_hours * 3600

    # ---------------------------------------------------------------- core --
    def _cache_path(self, path: str, params: dict) -> Path:
        public = {k: v for k, v in sorted(params.items()) if k.lower() != "apikey"}
        digest = hashlib.sha256(json.dumps([path, public], sort_keys=True).encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def get(self, path: str, params: dict | None = None, use_cache: bool = True) -> dict:
        params = dict(params or {})
        cache_file = self._cache_path(path, params)
        if use_cache and cache_file.exists() and time.time() - cache_file.stat().st_mtime < self.ttl:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        url = f"{self.base}{path}"
        params["apiKey"] = self._api_key
        backoff = 1.0
        for attempt in range(1, SETTINGS.umls.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=SETTINGS.umls.request_timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                log.warning("Network error on %s (attempt %d): %s", path, attempt, type(exc).__name__)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 401 or resp.status_code == 403:
                raise UmlsAuthError(f"UMLS API rejected the API key (HTTP {resp.status_code}) for {path}. "
                                    "Check the key in your UTS profile and that your UMLS license is active.")
            if resp.status_code == 404:
                return {"result": None, "_status": 404}
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", backoff))
                log.warning("HTTP %s on %s; retrying in %.1fs (attempt %d)", resp.status_code, path, wait, attempt)
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code != 200:
                raise UmlsApiError(f"HTTP {resp.status_code} for {path}")
            data = resp.json()
            if use_cache:
                cache_file.write_text(json.dumps(data), encoding="utf-8")
            return data
        raise UmlsApiError(f"Giving up on {path} after {SETTINGS.umls.max_retries} attempts")

    def paged(self, path: str, params: dict | None = None, max_pages: int = 50):
        """Iterate results across pages. /search ends with a 'NONE' sentinel;
        /content endpoints report pageCount."""
        params = dict(params or {})
        page = 1
        while page <= max_pages:
            params["pageNumber"] = page
            data = self.get(path, params)
            result = data.get("result")
            if result is None:
                return
            items = result.get("results") if isinstance(result, dict) and "results" in result else result
            if not items:
                return
            if isinstance(items, list):
                real = [i for i in items if i.get("ui") != "NONE"]
                yield from real
                if len(real) < len(items):  # NONE sentinel reached
                    return
            else:
                yield items
                return
            page_count = data.get("pageCount")
            if page_count and page >= page_count:
                return
            page += 1

    # ------------------------------------------------------------- helpers --
    def search(self, string: str, sabs: str | None = None, return_id_type: str = "code",
               search_type: str = "words", max_pages: int = 4) -> list[dict]:
        params = {"string": string, "returnIdType": return_id_type, "searchType": search_type, "pageSize": 100}
        if sabs:
            params["sabs"] = sabs
        return list(self.paged("/search/current", params, max_pages=max_pages))

    def source_concept(self, sab: str, code: str) -> dict | None:
        data = self.get(f"/content/current/source/{sab}/{code}")
        return data.get("result")

    def current_release(self) -> str | None:
        data = self.get("/content/current/CUI/C0009378")  # 'Colonoscopy' (CUI used only as probe)
        res = data.get("result") or {}
        uri = res.get("atoms", "") if isinstance(res, dict) else ""
        # URI looks like https://uts-ws.nlm.nih.gov/rest/content/2026AA/CUI/...
        parts = uri.split("/content/")
        return parts[1].split("/")[0] if len(parts) > 1 else None

    def verify_access(self, sabs=("CPT", "HCPCS", "HCPT", "SNOMEDCT_US"), probe: str = "colonoscopy") -> AccessReport:
        """Authenticate and check whether searches restricted to each vocabulary
        return results for a common procedure word. An empty result for CPT
        while other vocabularies return results suggests the account is not
        authorized for that source (or the probe term is absent)."""
        vocab = {}
        # The first call proves authentication (raises UmlsAuthError if bad).
        release = self.current_release()
        for sab in sabs:
            try:
                results = self.search(probe, sabs=sab, max_pages=1)
                vocab[sab] = {"accessible": bool(results), "results": len(results),
                              "note": "" if results else "no results returned - check license/affiliation or probe term"}
            except UmlsApiError as exc:
                vocab[sab] = {"accessible": False, "results": 0, "note": str(exc)}
        return AccessReport(authenticated=True, vocabularies=vocab, release=release)
