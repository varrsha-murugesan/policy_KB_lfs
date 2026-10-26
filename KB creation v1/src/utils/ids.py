"""Deterministic identifiers: same inputs -> same IDs on every run."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path


def stable_id(*parts, length: int = 20) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "unknown").lower()).strip("_") or "unknown"


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def json_sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def new_run_id(run_type: str) -> str:
    return f"{run_type}-{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
