"""Stage 3 - content-based CPT inference.

Builds one TextItem per policy section (boilerplate sentences and empty
sections excluded) and runs the shared TermMatcher. Section-level context
(section_seq, hierarchy_path, section_role) travels with every piece of
evidence so a code can later be tied to the rules of the same section."""
from __future__ import annotations

from src.mapping.term_matcher import MatchResult, TermMatcher, TextItem, policy_abbreviations


def content_items(policy, boilerplate: set[str] | None = None, include_attachments: bool = False) -> list[TextItem]:
    from src.utils.text_normalizer import normalize, split_sentences

    abbr = policy_abbreviations(policy)
    items = []
    for s in policy.sections:
        if not s.content or not s.content.strip():
            continue
        text = s.content
        if boilerplate:
            kept = [sent for _a, _b, sent in split_sentences(text) if normalize(sent) not in boilerplate]
            if not kept:
                continue
            text = "\n".join(kept)
        loc = getattr(s, "evidence_location", "policy_text")
        if loc == "attachment" and not include_attachments:
            continue
        source = {"policy_qa": "policy_qa", "attachment": "attachment"}.get(loc, "policy_content")
        items.append(TextItem(policy.policy_uid, source, s.seq, s.hierarchy_path, s.section_role, text, abbr))
    return items


def map_content(matcher: TermMatcher, policies, boilerplate=None, doc_freq=None, include_attachments=False) -> MatchResult:
    items = [it for p in policies for it in content_items(p, (boilerplate or {}).get(p.payer), include_attachments)]
    return matcher.match(items, doc_freq)
