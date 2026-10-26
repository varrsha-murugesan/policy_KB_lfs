"""Check that your UMLS API key works and which code vocabularies your account can see.

    python scripts/verify_umls_access.py

What it does: one authenticated call to learn the current release, then a
vocabulary-restricted search for a common procedure word in CPT, HCPCS, HCPT
and SNOMEDCT_US. It prints a table; it never prints the key.
Exit code 0 = authenticated and CPT accessible, 1 = authenticated but CPT not
visible, 2 = authentication/configuration problem.
"""
import sys

import _bootstrap  # noqa: F401
from config.settings import SETTINGS
from src.umls.api_client import UmlsApiClient, UmlsApiError, UmlsAuthError
from src.utils.logging_config import setup_logging


def main():
    log = setup_logging("verify_umls_access", SETTINGS.paths.logs_dir)
    try:
        client = UmlsApiClient()
        report = client.verify_access()
    except UmlsAuthError as exc:
        log.error("Authentication problem: %s", exc)
        sys.exit(2)
    except (UmlsApiError, OSError) as exc:
        log.error("Could not reach the UMLS API (%s). Check your network/proxy and retry.", exc)
        sys.exit(2)
    log.info("Authenticated. Current UMLS release reported by the API: %s", report.release or "unknown")
    for sab, info in report.vocabularies.items():
        log.info("  %-12s %-12s results=%-4s %s", sab, "ACCESSIBLE" if info["accessible"] else "NOT VISIBLE",
                 info["results"], info["note"])
    if not report.ok_for("CPT"):
        log.warning("CPT is not visible to this account. Check your UMLS license status in your UTS profile. "
                    "The pipeline still runs with HCPCS only, but CPT inference will be empty.")
        sys.exit(1)
    log.info("OK - credentials work and CPT content is accessible. Next: python scripts/download_umls_data.py")


if __name__ == "__main__":
    main()
