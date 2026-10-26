"""Download the current UMLS Metathesaurus release and extract only the needed RRF files.

    python scripts/download_umls_data.py                 # current full-subset release -> data/raw
    python scripts/download_umls_data.py --with-mrsat     # also extract MRSAT (very large; for CPT full descriptions)
    python scripts/download_umls_data.py --zip C:\\path\\umls-2026AA-metathesaurus-full.zip   # use a zip you downloaded in a browser

Interrupted downloads resume automatically when re-run (HTTP Range).
Files land in data/raw/ (zip) and data/raw/META/ (RRF). Both are git-ignored:
they are licensed content and must not be committed or shared.
"""
import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.umls.api_client import UmlsAuthError
from src.umls.downloader import DownloadError, download_release, extract_rrf_files, find_current_release
from src.utils.logging_config import setup_logging


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", help="use an already-downloaded release zip instead of downloading")
    ap.add_argument("--release-type", default=SETTINGS.umls.release_type)
    ap.add_argument("--with-mrsat", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    log = setup_logging("download_umls_data", SETTINGS.paths.logs_dir)
    try:
        if args.zip:
            zip_path = Path(args.zip)
            if not zip_path.exists():
                log.error("Zip not found: %s", zip_path)
                sys.exit(2)
        else:
            rel = find_current_release(args.release_type)
            log.info("Current release: %s (%s) %s", rel.get("releaseVersion") or rel.get("name"),
                     rel.get("releaseDate"), rel.get("fileName") or "")
            zip_path = download_release(rel["downloadUrl"], SETTINGS.paths.raw_dir)
        names = tuple(SETTINGS.umls.required_rrf) + ("MRDEF", "MRRANK") + (("MRSAT",) if args.with_mrsat else ())
        files = extract_rrf_files(zip_path, SETTINGS.paths.meta_dir, names, overwrite=args.overwrite)
    except UmlsAuthError as exc:
        log.error("%s", exc)
        sys.exit(2)
    except (DownloadError, OSError) as exc:
        log.error("%s - re-run the same command to resume.", exc)
        sys.exit(1)
    log.info("Extracted %d RRF file(s) into %s", len(files), SETTINGS.paths.meta_dir)
    log.info("Next: python scripts/build_knowledge_base.py")


if __name__ == "__main__":
    main()
