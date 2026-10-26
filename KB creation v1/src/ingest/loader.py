"""Load any payer's extracted JSON into the common policy model.

Profile resolution order: explicit profile -> profile matching --payer ->
profile whose detection patterns dominate the text -> generic profile built
from --payer with structure auto-detection. The source file is only read.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

from src.ingest.base import AdapterContext, finalize_records
from src.ingest.profiles import PayerProfile, load_profiles, resolve_profile
from src.ingest.registry import detect_adapter, get_adapter
from src.policy.json_parser import WRAPPER_KEYS, PolicyParseError, load_json_records
from src.utils.ids import file_sha256, json_sha256

log = logging.getLogger(__name__)


def _profile_for(path: Path, payer: str | None, profile, sample_text: str) -> tuple[PayerProfile, str]:
    """Returns (profile, payer_source) where payer_source records HOW the payer was decided."""
    if isinstance(profile, PayerProfile):
        return profile, "profile"
    if profile:
        return resolve_profile(str(profile)), "profile"
    if payer:
        try:
            return resolve_profile(payer), "cli"
        except FileNotFoundError:
            return PayerProfile(payer=payer, adapter="auto"), "cli"
    hits = {p.path: p.detection_hits(sample_text) for p in load_profiles()}
    hits = {k: v for k, v in hits.items() if v}
    if hits:
        best = max(sorted(hits), key=hits.get)
        others = sorted(hits.values(), reverse=True)[1:] or [0]
        if hits[best] >= 2 * max(others[0], 1) or others[0] == 0:
            return PayerProfile.from_file(Path(best)), "detected"
    raise PolicyParseError(f"Cannot determine the payer of {path.name}. Pass --payer-profile <profile> or --payer <name>.")


def load_policies(path: Path, payer: str | None = None, profile=None):
    path = Path(path)
    if not path.exists():
        raise PolicyParseError(f"Input file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.suffix.lower() == ".json" else None
    sample = path.read_text(encoding="utf-8-sig", errors="ignore")[:2_000_000]
    prof, payer_source = _profile_for(path, payer, profile, sample)
    if prof.adapter == "declarative":
        adapter = get_adapter("declarative")
        records = adapter.records_from(raw, prof.adapter_options)
    else:
        records = load_json_records(path)
        if prof.adapter == "auto":
            adapter, scores = detect_adapter(records)
            if adapter is None:
                raise PolicyParseError(f"No adapter recognizes {path.name} (scores {scores}). Write a payer profile "
                                       "using the declarative adapter (docs/ADDING_A_PAYER.md).")
        else:
            adapter = get_adapter(prof.adapter)
            if adapter.detect(records) < 0.5:
                alt, scores = detect_adapter(records)
                if alt is None:
                    raise PolicyParseError(f"Profile '{prof.payer}' uses adapter '{prof.adapter}', which does not "
                                           f"recognize {path.name} (scores {scores}).")
                log.warning("Profile '%s' expects adapter '%s' but %s has the structure of '%s'; using '%s'.",
                            prof.payer, prof.adapter, path.name, alt.name, alt.name)
                adapter = alt
    if not records:
        raise PolicyParseError(f"{path.name}: no records found for profile {prof.payer}")
    sha = file_sha256(path)
    doc_id = sha[:20]
    ctx = AdapterContext(prof, doc_id)
    policies = adapter.parse(records, ctx)
    warnings = ctx.warnings + finalize_records(policies, prof, payer_source)
    for p in policies:
        p.source_document_id = doc_id
        p.adapter = p.adapter or f"{adapter.name}/{adapter.version}"
        p.content_sha256 = json_sha256({"title": p.policy_title, "sections": [s.content for s in p.sections],
                                        "lists": [l["codes"] for l in p.upstream_code_lists]})
    report = {
        "file": path.name, "records": len(records), "policies": len(policies), "adapter": f"{adapter.name}/{adapter.version}",
        "payer": prof.payer, "payer_source": payer_source, "profile": prof.path or "generated",
        "record_status": dict(Counter(p.record_status for p in policies)),
        "title_quality": dict(Counter(p.title_quality for p in policies)),
        "identifier_quality": dict(Counter(p.identifier_quality for p in policies)),
        "sections": sum(len(p.sections) for p in policies),
        "sections_by_location": dict(Counter(s.evidence_location for p in policies for s in p.sections)),
        "code_list_entries": sum(len(l["codes"]) for p in policies for l in p.upstream_code_lists),
        "top_level_keys": sorted({k for r in records[:200] if isinstance(r, dict) for k in r}),
        "warnings": warnings[:200],
    }
    source_doc = {"source_document_id": doc_id, "payer": prof.payer, "file_name": path.name, "file_sha256": sha,
                  "adapter": report["adapter"], "payer_profile": prof.path or None, "n_records": len(records),
                  "schema_report": report}
    log.info("Loaded %s with profile '%s' / adapter %s: %d policies, statuses %s", path.name, prof.payer,
             report["adapter"], len(policies), report["record_status"])
    return policies, source_doc
