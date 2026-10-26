"""Stage 4 - title-based CPT inference, run independently of content mapping.

Titles are short and high-signal, so fuzzy matching is enabled for them by
default (bounded and low-scored). Generic title prefixes such as
"Laboratory -" are kept as text but never match on their own because single
generic words are filtered by the window builder."""
from __future__ import annotations

from src.mapping.term_matcher import MatchResult, TermMatcher, TextItem, policy_abbreviations


def title_items(policy) -> list[TextItem]:
    if not policy.policy_title or not policy.policy_title.strip():
        return []
    if getattr(policy, "title_quality", "specific") != "specific":   # generic headers are not evidence
        return []
    return [TextItem(policy.policy_uid, "policy_title", None, "TITLE", "title", policy.policy_title,
                     policy_abbreviations(policy))]


def map_titles(matcher: TermMatcher, policies) -> MatchResult:
    items = [it for p in policies for it in title_items(p)]
    return matcher.match(items, None)
