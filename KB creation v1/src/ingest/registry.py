"""Adapter registry. New adapters register here; nothing else in the engine changes."""
from __future__ import annotations

from src.ingest.adapters.declarative import DeclarativeAdapter
from src.ingest.adapters.guidelines_document import GuidelinesDocumentAdapter
from src.ingest.adapters.sectioned_list import SectionedListAdapter
from src.ingest.base import BaseAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    a.name: a for a in (SectionedListAdapter, GuidelinesDocumentAdapter, DeclarativeAdapter)
}


def get_adapter(name: str) -> BaseAdapter:
    if name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{name}'. Registered: {sorted(ADAPTERS)}")
    return ADAPTERS[name]()


def detect_adapter(records: list) -> tuple[BaseAdapter | None, dict]:
    scores = {n: a().detect(records) for n, a in ADAPTERS.items()}
    best = max(sorted(scores), key=scores.get)
    return (ADAPTERS[best]() if scores[best] >= 0.8 else None), scores
