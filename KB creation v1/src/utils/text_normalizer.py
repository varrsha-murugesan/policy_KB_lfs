"""Deterministic text normalization used identically for KB terms and policy text.

The same functions MUST be applied to both sides of a match; otherwise exact
matching silently degrades. Nothing here invents synonyms: normalization only
changes case, unicode forms, punctuation and (for the bag key) word order and
simple plural endings.
"""
from __future__ import annotations

import re
import unicodedata

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"'}
_GREEK = {"β": "beta", "α": "alpha", "γ": "gamma", "δ": "delta", "κ": "kappa", "λ": "lambda"}

# Removed only when building the order-insensitive bag key. "with"/"without"
# are deliberately KEPT because they change CPT meaning ("with biopsy").
BAG_STOPWORDS = frozenset({"a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "by", "per", "s"})

# Words that never constitute evidence of a procedure on their own.
GENERIC_WORDS = frozenset(
    """service services procedure procedures test tests testing therapy treatment care visit
    visits code codes claim claims policy policies payment payments billing bill provider providers
    member members plan plans medicare medicaid advantage humana unitedhealthcare uhc medical
    reimbursement guidelines requirements requirement other unlisted each additional initial
    subsequent per day days report reports patient patients program products product""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENT_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z0-9•(\"'])|\s*•\s*|\n+")


def basic_clean(text: str | None) -> str:
    """Unicode/punctuation cleanup that preserves case and character offsets
    as closely as practical (used for display and code extraction)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_DASHES).translate(_QUOTES)
    t = t.replace("®", "").replace("™", "")
    for g, rep in _GREEK.items():
        t = t.replace(g, rep)
    return t


def normalize(text: str | None) -> str:
    """Lowercase, unify punctuation, drop possessives, collapse whitespace."""
    t = basic_clean(text).lower()
    t = t.replace("®", " ").replace("™", " ")
    t = re.sub(r"'s\b", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str | None) -> list[str]:
    return normalize(text).split()


def _singular(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("ses"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def bag_key(text: str | None) -> str:
    """Order-insensitive key: normalized, stopwords removed, light singularization,
    sorted unique tokens. 'testing for hepatitis' == 'hepatitis testing'? No:
    'testing' != 'test'; we do not stem verbs, only plurals."""
    toks = sorted({_singular(t) for t in tokens(text) if t not in BAG_STOPWORDS})
    return " ".join(toks)


def descriptor_head(text: str | None) -> str:
    """Leading clause of a code descriptor, before the first , ; ( or ':'.
    'Colonoscopy, flexible; diagnostic' -> 'colonoscopy'. This is a lexical
    key derived from the source string, not an invented synonym."""
    if not text:
        return ""
    head = re.split(r"[,;(:]", basic_clean(text), maxsplit=1)[0]
    return normalize(head)


def is_generic_phrase(norm_phrase: str) -> bool:
    toks = norm_phrase.split()
    return not toks or all(t in GENERIC_WORDS or t in BAG_STOPWORDS or t.isdigit() for t in toks)


def split_sentences(text: str | None) -> list[tuple[int, int, str]]:
    """Split into sentences/bullets. Returns (start, end, sentence) with offsets
    into the basic_clean()ed text (same length as the original for most input)."""
    t = basic_clean(text)
    out, pos = [], 0
    for m in _SENT_SPLIT_RE.finditer(t):
        seg = t[pos:m.start()]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            out.append((pos + lead, m.start(), seg.strip()))
        pos = m.end()
    seg = t[pos:]
    if seg.strip():
        lead = len(seg) - len(seg.lstrip())
        out.append((pos + lead, len(t), seg.strip()))
    return out


def token_spans(text: str) -> list[tuple[str, int, int]]:
    """Normalized tokens with character offsets into basic_clean(text).lower()."""
    low = basic_clean(text).lower()
    low = re.sub(r"'s\b", "  ", low)  # keep offsets: replace with same length
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(low)]


_ABBR_RE = re.compile(r"([A-Za-z][A-Za-z\-/ ]{2,80}?)\s*\(\s*([A-Za-z][A-Za-z0-9/&\-]{1,12})\s*\)")


def find_abbreviations(text: str) -> dict[str, str]:
    """Policy-local abbreviation definitions of the form 'long form (ABBR)'.
    The long form is accepted only if its initial letters can spell the
    abbreviation (Schwartz-Hearst style check), so arbitrary parentheses are
    ignored. Returns {abbr_normalized: long_form_normalized}."""
    found: dict[str, str] = {}
    for m in _ABBR_RE.finditer(basic_clean(text)):
        long_words = normalize(m.group(1)).split()
        abbr = m.group(2)
        letters = [c.lower() for c in abbr if c.isalpha()]
        if not letters or len(letters) > 10:
            continue
        # take the shortest suffix of long_words whose initials cover the letters in order
        for k in range(1, min(len(long_words), len(letters) + 3) + 1):
            cand = long_words[-k:]
            initials = "".join(w[0] for w in cand)
            it = iter(initials)
            if all(ch in it for ch in letters) and cand[0][0] == letters[0]:
                found[normalize(abbr)] = " ".join(cand)
                break
    return found
