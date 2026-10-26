"""Data-quality assessment of extracted policies.

Flags let downstream gap analysis say "cannot assess" instead of "missing"
when the extraction itself looks incomplete (Scenario J)."""
from __future__ import annotations

import re

TERMINAL = re.compile(r"[.!?:;)\]\"'%\u201d\u2019]\s*$")


def _looks_truncated(text: str) -> bool:
    t = re.sub(r"[\s\u2022]+$", "", text or "")
    if len(t) <= 150 or TERMINAL.search(t):
        return False
    last = t.rsplit("\u2022", 1)
    if len(last) == 2 and len(last[1].strip()) < 120:  # ends inside a bullet list item, not a sentence
        return False
    return True


def quality_flags(policy) -> list[str]:
    flags = []
    secs = policy.sections
    if not policy.policy_title:
        flags.append("missing_title")
    if not secs:
        flags.append("no_sections")
        return flags
    if len(secs) < 3:
        flags.append("few_sections")
    empty = sum(1 for s in secs if not (s.content or "").strip())
    if empty:
        flags.append(f"empty_sections:{empty}")
    if not any(s.section_role == "payment_policy" for s in secs):
        flags.append("no_payment_policy_section")
    trunc = sum(1 for s in secs if s.content_type == "paragraph" and _looks_truncated(s.content))
    if trunc:
        flags.append(f"possible_truncated_paragraphs:{trunc}")
    total_chars = sum(len(s.content or "") for s in secs)
    if total_chars < 400:
        flags.append("very_short_text")
    return flags


def incomplete(flags: list[str]) -> bool:
    """True when flags indicate the extraction may be missing rule content."""
    return any(f.startswith(("no_sections", "few_sections", "no_payment_policy_section", "very_short_text",
                             "possible_truncated_paragraphs", "empty_sections")) for f in flags or [])
