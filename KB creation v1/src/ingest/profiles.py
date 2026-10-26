"""Payer profiles: everything payer-specific lives in config/payers/*.json.

A profile names the payer, chooses an adapter and gives it options. The
shared engine never contains payer names or payer formats. Adding a payer =
adding a profile (and, only for a genuinely new JSON shape that the
declarative adapter cannot describe, a new adapter class).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import PROJECT_ROOT

PROFILE_DIR = PROJECT_ROOT / "config" / "payers"


@dataclass
class PayerProfile:
    payer: str
    adapter: str
    path: str = ""
    detection_patterns: list[str] = field(default_factory=list)   # regexes identifying the payer in text
    id_fields: list[str] = field(default_factory=list)           # canonical fields usable as stable record ids
    generic_title_patterns: list[str] = field(default_factory=list)
    section_role_patterns: dict = field(default_factory=dict)    # role -> regex over section names
    scope_label_pattern: str | None = None
    adapter_options: dict = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "PayerProfile":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "path"}
        return cls(path=str(path), **known)

    def detection_hits(self, text: str) -> int:
        return sum(len(re.findall(p, text)) for p in self.detection_patterns)


def load_profiles(extra_dirs: list[Path] | None = None) -> list[PayerProfile]:
    out = []
    for d in [PROFILE_DIR, *(extra_dirs or [])]:
        for p in sorted(Path(d).glob("*.json")):
            out.append(PayerProfile.from_file(p))
    return out


def resolve_profile(name_or_path: str, extra_dirs: list[Path] | None = None) -> PayerProfile:
    """Accept a profile file path, a profile file stem ('humana') or a payer name."""
    p = Path(name_or_path)
    if p.suffix == ".json" and p.exists():
        return PayerProfile.from_file(p)
    for prof in load_profiles(extra_dirs):
        if Path(prof.path).stem.lower() == name_or_path.lower() or prof.payer.lower() == name_or_path.lower():
            return prof
    raise FileNotFoundError(f"No payer profile '{name_or_path}' in {PROFILE_DIR}. Create one (see docs/ADDING_A_PAYER.md).")
