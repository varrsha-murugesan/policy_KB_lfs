"""Download a UMLS Metathesaurus release with the UTS Download API and extract
only the RRF files this project needs.

- Release discovery: GET https://uts-ws.nlm.nih.gov/releases?releaseType=...&current=true
- Download:          GET https://uts-ws.nlm.nih.gov/download?url=<downloadUrl>&apiKey=<key>
  (documented at https://documentation.uts.nlm.nih.gov/automating-downloads.html)

Resumability: data streams into <file>.part. If interrupted, the next run sends
an HTTP Range header and appends. If the server ignores Range (HTTP 200), the
partial file is discarded and the download restarts - never silently corrupted.
A manifest JSON records the expected size so a finished file is verified.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

import requests

from config.settings import SETTINGS
from src.umls.api_client import UmlsAuthError, get_api_key

log = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


def find_current_release(release_type: str | None = None, session: requests.Session | None = None) -> dict:
    s = session or requests.Session()
    resp = s.get(SETTINGS.umls.releases_url, params={"releaseType": release_type or SETTINGS.umls.release_type,
                                                    "current": "true"}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("result") or data.get("releases") or []
    if not items:
        raise DownloadError(f"No current release found for releaseType={release_type}")
    rel = items[0]
    if not rel.get("downloadUrl"):
        raise DownloadError(f"Release entry has no downloadUrl: {sorted(rel)}")
    return rel


def download_release(download_url: str, dest_dir: Path, session: requests.Session | None = None,
                     chunk_size: int = 1 << 22, max_attempts: int = 8) -> Path:
    """Resumable download. Never logs the API key (only the public file URL)."""
    s = session or requests.Session()
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = download_url.rstrip("/").split("/")[-1]
    final = dest_dir / filename
    part = dest_dir / (filename + ".part")
    manifest = dest_dir / (filename + ".manifest.json")
    if final.exists() and manifest.exists():
        expected = json.loads(manifest.read_text()).get("size")
        if expected and final.stat().st_size == expected:
            log.info("Already downloaded and verified: %s", final.name)
            return final
    api_key = get_api_key()
    attempt, backoff = 0, 2.0
    while attempt < max_attempts:
        attempt += 1
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        log.info("Downloading %s (attempt %d, resume from %d bytes)", filename, attempt, have)
        try:
            with s.get(SETTINGS.umls.download_url, params={"url": download_url, "apiKey": api_key},
                       headers=headers, stream=True, timeout=120, allow_redirects=True) as resp:
                if resp.status_code in (401, 403):
                    raise UmlsAuthError(f"Download refused (HTTP {resp.status_code}). Check the API key and that "
                                        "your UMLS license is active.")
                if resp.status_code == 416:  # requested range not satisfiable -> already complete
                    break
                if resp.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status_code} while downloading {filename}")
                ctype = resp.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    raise DownloadError("Server returned an HTML page instead of the file (usually a login/"
                                        "license page). Verify the API key and license status in UTS.")
                mode = "ab"
                if resp.status_code == 200 and have:
                    log.warning("Server ignored Range; restarting download from zero")
                    mode = "wb"
                total = None
                if resp.headers.get("Content-Range"):
                    total = int(resp.headers["Content-Range"].split("/")[-1])
                elif resp.headers.get("Content-Length"):
                    total = int(resp.headers["Content-Length"]) + (have if resp.status_code == 206 else 0)
                if total:
                    manifest.write_text(json.dumps({"url": download_url, "size": total}))
                last_log = time.monotonic()
                with open(part, mode) as fh:
                    for block in resp.iter_content(chunk_size=chunk_size):
                        fh.write(block)
                        if time.monotonic() - last_log > 30:
                            log.info("  %.1f MB written", part.stat().st_size / 1e6)
                            last_log = time.monotonic()
            break
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            log.warning("Network interruption (%s); will resume in %.0fs", type(exc).__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
    else:
        raise DownloadError(f"Download of {filename} failed after {max_attempts} attempts; rerun to resume.")
    expected = json.loads(manifest.read_text()).get("size") if manifest.exists() else None
    if expected and part.stat().st_size != expected:
        raise DownloadError(f"Size mismatch for {filename}: {part.stat().st_size} != {expected}. Rerun to resume.")
    part.replace(final)
    log.info("Download complete: %s (%.1f MB)", final.name, final.stat().st_size / 1e6)
    return final


def extract_rrf_files(zip_path: Path, meta_dir: Path, names: tuple, overwrite: bool = False) -> list[Path]:
    """Extract only the requested RRF tables (including split parts such as
    MRCONSO.RRF.aa) from the release zip into meta_dir. Handles nested zips
    produced by some release packagings."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        inner_zips = [m for m in members if m.lower().endswith(".zip")]
        wanted = [m for m in members if _is_wanted(m, names)]
        if not wanted and inner_zips:
            for inner in inner_zips:
                tmp = meta_dir / Path(inner).name
                with zf.open(inner) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out += extract_rrf_files(tmp, meta_dir, names, overwrite)
                tmp.unlink(missing_ok=True)
            return out
        for m in wanted:
            target = meta_dir / Path(m).name
            if target.exists() and not overwrite and target.stat().st_size == zf.getinfo(m).file_size:
                log.info("Already extracted: %s", target.name)
                out.append(target)
                continue
            log.info("Extracting %s (%.1f MB)", Path(m).name, zf.getinfo(m).file_size / 1e6)
            tmp = target.with_suffix(target.suffix + ".part")
            with zf.open(m) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 22)
            tmp.replace(target)
            out.append(target)
    missing = [n for n in names if not any(p.name.startswith(n + ".RRF") for p in out)]
    if missing:
        log.warning("Not found in archive: %s", missing)
    return out


def _is_wanted(member: str, names: tuple) -> bool:
    base = Path(member).name
    return any(base == f"{n}.RRF" or base.startswith(f"{n}.RRF.") for n in names) and "/META/" in f"/{member}"
