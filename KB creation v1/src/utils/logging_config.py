"""Logging setup. A redaction filter strips the UMLS API key (and any
apiKey=... query parameter) from every log record, so credentials cannot leak
into console output or log files even if a URL is logged by mistake."""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_APIKEY_RE = re.compile(r"(apiKey=|validatorApiKey=|apikey=)[^&\s'\"]+", re.IGNORECASE)


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        secret = os.environ.get("UMLS_API_KEY")
        red = _APIKEY_RE.sub(r"\1***REDACTED***", msg)
        if secret and len(secret) >= 8:
            red = red.replace(secret, "***REDACTED***")
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup_logging(name: str, logs_dir: Path | None = None, level: str | None = None) -> logging.Logger:
    level = (level or os.environ.get("PPI_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(RedactSecretsFilter())
    root.addHandler(console)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logs_dir / f"{name}-{datetime.now():%Y%m%d}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactSecretsFilter())
        root.addHandler(fh)
    return logging.getLogger(name)
