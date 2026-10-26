"""Stages 2-4 engine: deterministic terminology matching of policy text against the KB.

Used identically for policy content (content_mapper) and for the policy title
(title_mapper); only the evidence_source and the enabled methods differ.

Algorithm
1. Split text into sentences; tokenize; expand policy-local abbreviations
   ("chronic lower back pain (cLBP)" -> later "cLBP" also tried as the long form).
2. Build candidate windows of 1..N tokens. Windows that start/end with a
   stopword, contain only generic words, or are single short tokens are dropped.
3. Look windows up in the KB with set-based SQL joins:
      exact      window == terms.str_norm
      normalized window bag-key == terms.str_key (word order / plural only)
      head       window == terms.head_norm (leading clause of a code descriptor)
      fuzzy      (titles by default) rapidfuzz token_sort_ratio >= threshold over
                 candidates sharing all-but-one bag tokens
4. Resolve each matched atom to codes through explicit pathways:
      code-level atom of CPT/HCPCS          -> that code
      atom on a CUI that also carries codes -> those codes (UMLS-asserted synonymy)
      hierarchy heading (MTHCH/MTHHH/HT)    -> descendant codes (CHD, bounded depth)
      allowed relationship (RQ, SY, RN, CHD)-> related concept's codes
   Nothing is inferred from string similarity alone except the bounded fuzzy
   method, which is scored low and always flagged for review.
5. Longest-match selection per sentence among windows that produced codes;
   nested shorter matches are dropped. Matched-but-uncoded concepts are kept as
   policy_concepts; procedure-looking phrases with no match go to unmatched_terms.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import duckdb
import pandas as pd
from rapidfuzz import fuzz

from config.settings import SETTINGS
from src.mapping.confidence import evidence_score
from src.umls.rrf_loader import classify_code
from src.utils.text_normalizer import (BAG_STOPWORDS, GENERIC_WORDS, bag_key, find_abbreviations, is_generic_phrase,
                                       split_sentences, token_spans)

log = logging.getLogger(__name__)

EDGE_STOPWORDS = BAG_STOPWORDS | {"with", "without", "is", "are", "be", "as", "at", "from", "that", "this", "these",
                                  "those", "may", "must", "will", "not", "no", "any", "all"}
PROCEDURE_CUES = ("ectomy", "otomy", "ostomy", "oscopy", "plasty", "graphy", "centesis", "pexy", "rrhaphy", "lysis",
                  "biopsy", "testing", "test", "assay", "screening", "imaging", "injection", "infusion", "surgery",
                  "therapy", "transplant", "implant", "implantation", "ablation", "repair", "removal", "excision",
                  "evaluation", "examination", "management", "monitoring", "analysis", "panel", "culture")
UNMATCHED_LEADING_NOISE = {"modifier", "modifiers", "applicable", "appropriate", "other", "such", "same", "separate",
                           "specific", "certain", "related", "following", "each", "additional", "records", "record",
                           "visit", "history", "any", "all", "covered", "noncovered", "billed", "reported"}
METHOD_RANK = {"exact_preferred_term": 0, "exact_synonym": 1, "cross_vocab_synonym": 2, "normalized_phrase": 3,
               "descriptor_head": 4, "hierarchy_group": 5, "fuzzy": 6, "related_concept": 7}
RULE_ID = "term-match-2"
TTY_RANK = {"PT": 0, "ETCLIN": 1, "SY": 2, "ETCF": 3, "POS": 4, "AB": 8}


@dataclass
class TextItem:
    policy_uid: str
    evidence_source: str            # policy_title | policy_content
    section_seq: int | None
    hierarchy_path: str | None
    section_role: str | None
    text: str
    abbreviations: dict = field(default_factory=dict)


@dataclass
class Window:
    item_idx: int
    sent_idx: int
    tok_start: int
    tok_end: int                    # exclusive
    phrase_norm: str
    phrase_key: str
    char_start: int
    char_end: int
    raw_text: str
    abbreviation_expanded: str | None


@dataclass
class InferredEvidence:
    policy_uid: str
    evidence_source: str
    method: str
    code_system: str
    code: str
    code_sab: str
    section_seq: int | None
    hierarchy_path: str | None
    matched_phrase: str
    normalized_phrase: str
    abbreviation_expanded: str | None
    matched_term: str
    matched_term_tty: str
    matched_sab: str
    matched_aui: str
    matched_srl: int | None
    cui: str
    relationship_path: str | None
    char_start: int
    char_end: int
    candidate_set_size: int
    candidate_set_truncated: bool
    score: float
    rule_id: str = RULE_ID


@dataclass
class MatchResult:
    evidence: list[InferredEvidence] = field(default_factory=list)
    concepts: list[dict] = field(default_factory=list)      # recognized concepts (coded or not)
    unmatched: list[dict] = field(default_factory=list)


class TermMatcher:
    def __init__(self, con: duckdb.DuckDBPyConnection, fuzzy_sources: tuple = ("policy_title",)):
        self.con = con
        self.cfg = SETTINGS.matching
        self.vocab = SETTINGS.vocab
        self.fuzzy_sources = fuzzy_sources if self.cfg.fuzzy_enabled else ()
        self._fuzzy_ready = False

    # --------------------------------------------------------------- windows --
    def build_windows(self, items: list[TextItem]) -> list[Window]:
        wins: list[Window] = []
        max_n = self.cfg.max_phrase_tokens
        for ii, item in enumerate(items):
            for si, (s_start, _s_end, sent) in enumerate(split_sentences(item.text)):
                toks = []  # (token, char_start, char_end, expanded_from)
                for tok, cs, ce in token_spans(sent):
                    long_form = item.abbreviations.get(tok)
                    if long_form and tok not in GENERIC_WORDS:
                        for lt in long_form.split():
                            toks.append((lt, s_start + cs, s_start + ce, f"{tok} -> {long_form}"))
                    else:
                        toks.append((tok, s_start + cs, s_start + ce, None))
                for i in range(len(toks)):
                    for n in range(1, max_n + 1):
                        j = i + n
                        if j > len(toks):
                            break
                        span = toks[i:j]
                        words = [t[0] for t in span]
                        if words[0] in EDGE_STOPWORDS or words[-1] in EDGE_STOPWORDS:
                            continue
                        if not any(w.isalpha() for w in words):
                            continue
                        phrase = " ".join(words)
                        if is_generic_phrase(phrase):
                            continue
                        if n == 1 and (len(words[0]) < self.cfg.single_token_min_len or words[0] in GENERIC_WORDS):
                            continue
                        # an abbreviation expansion must be used whole, not split
                        exp = {t[3] for t in span if t[3]}
                        if exp and (i > 0 and toks[i - 1][3] in exp and toks[i - 1][1] == span[0][1]
                                    or j < len(toks) and toks[j][3] in exp and toks[j][1] == span[-1][1]):
                            continue
                        cs, ce = span[0][1], span[-1][2]
                        wins.append(Window(ii, si, i, j, phrase, bag_key(phrase), cs, ce,
                                           item.text[cs:ce] if ce <= len(item.text) else phrase,
                                           "; ".join(sorted(exp)) or None))
        return wins

    # --------------------------------------------------------------- lookups --
    def _lookup(self, wins: list[Window]) -> pd.DataFrame:
        phr = pd.DataFrame({"phrase_norm": [w.phrase_norm for w in wins], "phrase_key": [w.phrase_key for w in wins],
                            "n": [w.tok_end - w.tok_start for w in wins]}).drop_duplicates("phrase_norm")
        self.con.register("q_phr", phr)
        try:
            df = self.con.execute("""
                SELECT q.phrase_norm, 'exact' AS match_type, t.aui, t.cui, t.sab, t.code, t.tty, t.str, t.srl,
                       t.suppress, t.is_code_level, NULL::DOUBLE AS ratio
                FROM q_phr q JOIN terms t ON t.str_norm = q.phrase_norm
                UNION ALL
                SELECT q.phrase_norm, 'bag', t.aui, t.cui, t.sab, t.code, t.tty, t.str, t.srl, t.suppress, t.is_code_level, NULL
                FROM q_phr q JOIN terms t ON t.str_key = q.phrase_key
                WHERE q.n >= 2 AND t.str_norm <> q.phrase_norm
                UNION ALL
                SELECT q.phrase_norm, 'head', t.aui, t.cui, t.sab, t.code, t.tty, t.str, t.srl, t.suppress, t.is_code_level, NULL
                FROM q_phr q JOIN terms t ON t.head_norm = q.phrase_norm
                WHERE t.is_code_level AND t.str_norm <> q.phrase_norm
                ORDER BY 1, 2, 3
            """).df()
        finally:
            self.con.unregister("q_phr")
        return df

    def _prepare_fuzzy(self):
        if self._fuzzy_ready:
            return
        self.con.execute("""
            CREATE OR REPLACE TEMP TABLE fz_tok AS
            SELECT t.aui, tok, len(string_split(t.str_key, ' ')) AS ntok
            FROM terms t LEFT JOIN concepts c ON c.cui = t.cui,
                 unnest(string_split(t.str_key, ' ')) AS u(tok)
            WHERE (t.is_code_level OR c.in_procedure_lexicon OR c.is_code_bearing)
              AND len(string_split(t.str_key, ' ')) BETWEEN 2 AND 10""")
        self._fuzzy_ready = True

    def _fuzzy_lookup(self, wins: list[Window]) -> pd.DataFrame:
        cand = [w for w in wins if w.tok_end - w.tok_start >= self.cfg.fuzzy_min_tokens]
        if not cand:
            return pd.DataFrame()
        self._prepare_fuzzy()
        rows = [(w.phrase_norm, tok, len(set(w.phrase_key.split()))) for w in cand for tok in set(w.phrase_key.split())]
        q = pd.DataFrame(rows, columns=["phrase_norm", "tok", "nq"]).drop_duplicates()
        self.con.register("q_fz", q)
        try:
            pairs = self.con.execute("""
                SELECT q.phrase_norm, f.aui, COUNT(*) AS shared, any_value(q.nq) AS nq, any_value(f.ntok) AS nt
                FROM q_fz q JOIN fz_tok f ON f.tok = q.tok
                GROUP BY 1, 2
                HAVING COUNT(*) >= GREATEST(2, any_value(q.nq) - 1) AND abs(any_value(f.ntok) - any_value(q.nq)) <= 1
            """).df()
        finally:
            self.con.unregister("q_fz")
        if pairs.empty:
            return pd.DataFrame()
        terms = self.con.execute(
            "SELECT aui, cui, sab, code, tty, str, str_norm, srl, suppress, is_code_level FROM terms WHERE aui IN "
            "(SELECT unnest(?))", [list(pairs.aui.unique())]).df().set_index("aui")
        out = []
        for phrase, grp in pairs.groupby("phrase_norm"):
            scored = []
            for aui in grp.aui:
                t = terms.loc[aui]
                if t.str_norm == phrase:
                    continue
                r = fuzz.token_sort_ratio(phrase, t.str_norm)
                if r >= self.cfg.fuzzy_threshold:
                    scored.append((r, aui, t))
            for r, aui, t in sorted(scored, key=lambda x: (-x[0], x[1]))[: self.cfg.max_fuzzy_candidates_per_window]:
                out.append({"phrase_norm": phrase, "match_type": "fuzzy", "aui": aui, "cui": t.cui, "sab": t.sab,
                            "code": t.code, "tty": t.tty, "str": t.str, "srl": t.srl, "suppress": t.suppress,
                            "is_code_level": bool(t.is_code_level), "ratio": float(r)})
        return pd.DataFrame(out)

    # ------------------------------------------------------- code resolution --
    def _resolve(self, cuis: set[str]) -> tuple[dict, dict, dict]:
        """Return (same_cui_codes, hierarchy_codes, related_codes) keyed by CUI.
        Values are lists of (sab, code[, path])."""
        same, hier, related = defaultdict(list), defaultdict(list), defaultdict(list)
        if not cuis:
            return same, hier, related
        cl = list(cuis)
        for cui, sab, code in self.con.execute(
                "SELECT cui, sab, code FROM v_code_concepts WHERE cui IN (SELECT unnest(?))", [cl]).fetchall():
            same[cui].append((sab, code))
        depth = int(self.cfg.max_hierarchy_depth)
        for root, sab, code in self.con.execute(f"""
                WITH RECURSIVE d(root, cui, depth) AS (
                    SELECT c, c, 0 FROM (SELECT unnest(?) AS c)
                    UNION
                    SELECT d.root, r.cui2, d.depth + 1 FROM d JOIN concept_relationships r
                      ON r.cui1 = d.cui AND r.rel = 'CHD' WHERE d.depth < {depth})
                SELECT DISTINCT d.root, v.sab, v.code FROM d JOIN v_code_concepts v ON v.cui = d.cui
                WHERE d.depth > 0""", [cl]).fetchall():
            hier[root].append((sab, code))
        rels = list(self.vocab.candidate_rels)
        for c1, rel, rela, c2, sab, code in self.con.execute("""
                SELECT r.cui1, r.rel, r.rela, r.cui2, v.sab, v.code FROM concept_relationships r
                JOIN v_code_concepts v ON v.cui = r.cui2
                WHERE r.cui1 IN (SELECT unnest(?)) AND r.rel IN (SELECT unnest(?))""", [cl, rels]).fetchall():
            related[c1].append((sab, code, f"{c1} -{rel}{'/' + rela if rela else ''}-> {c2}"))
        return same, hier, related

    @staticmethod
    def _dedupe_codes(pairs):
        """Collapse CPT/HCPT duplicates: key on (code_system, code); prefer CPT over HCPT."""
        best = {}
        for p in pairs:
            sab, code = p[0], p[1]
            cls = classify_code(code)
            if not cls:
                continue
            key = (cls[0], code)
            if key not in best or (best[key][0] == "HCPT" and sab == "CPT"):
                best[key] = p
        return best

    # ------------------------------------------------------------------ main --
    def match(self, items: list[TextItem], corpus_doc_freq: dict[str, float] | None = None) -> MatchResult:
        result = MatchResult()
        if not items:
            return result
        wins = self.build_windows(items)
        if not wins:
            return result
        hits = self._lookup(wins)
        fuzzy_wins = [w for w in wins if items[w.item_idx].evidence_source in self.fuzzy_sources]
        exact_phr = set(hits.phrase_norm) if not hits.empty else set()
        fz = self._fuzzy_lookup([w for w in fuzzy_wins if w.phrase_norm not in exact_phr])
        if not fz.empty:
            hits = pd.concat([hits, fz], ignore_index=True)
        if hits.empty:
            self._collect_unmatched(items, wins, set(), result)
            return result
        generic_df = corpus_doc_freq or {}
        too_common = {p for p, f in generic_df.items() if f > self.cfg.generic_phrase_doc_fraction}
        hits = hits[~hits.phrase_norm.isin(too_common)]

        same, hier, related = self._resolve(set(hits.cui))
        hier_sabs = set(self.vocab.hierarchy_sabs)

        # per phrase: list of candidate groups (one per matched atom)
        groups_by_phrase: dict[str, list[dict]] = defaultdict(list)
        for h in hits.itertuples(index=False):
            mt = h.match_type
            codes, method, path = {}, None, None
            if h.is_code_level:
                codes = self._dedupe_codes([(h.sab, h.code)])
                method = {"exact": "exact_preferred_term" if h.tty == "PT" else "exact_synonym",
                          "bag": "normalized_phrase", "head": "descriptor_head", "fuzzy": "fuzzy"}[mt]
            elif same.get(h.cui):
                codes = self._dedupe_codes(same[h.cui])
                method = {"exact": "cross_vocab_synonym", "bag": "normalized_phrase", "fuzzy": "fuzzy"}.get(mt)
            elif hier.get(h.cui) and (h.sab in hier_sabs or h.tty in ("HT", "MTH_HT")):
                codes = self._dedupe_codes(hier[h.cui])
                method = "hierarchy_group" if mt != "fuzzy" else "fuzzy"
                path = f"{h.cui} -CHD*-> descendants"
            elif related.get(h.cui):
                rel_list = related[h.cui]
                codes = self._dedupe_codes(rel_list)
                method = "related_concept" if mt != "fuzzy" else "fuzzy"
                path = "; ".join(sorted({r[2] for r in rel_list}))[:500]
            if method is None and mt == "head":
                continue
            groups_by_phrase[h.phrase_norm].append({"hit": h, "codes": codes, "method": method or "recognized_no_code_link",
                                                    "path": path})

        # longest-match selection per (item, sentence)
        by_sent: dict[tuple, list[Window]] = defaultdict(list)
        for w in wins:
            if w.phrase_norm in groups_by_phrase:
                by_sent[(w.item_idx, w.sent_idx)].append(w)
        covered_any: set[tuple] = set()
        for (ii, si), ws in by_sent.items():
            item = items[ii]
            productive = [w for w in ws if any(g["codes"] for g in groups_by_phrase[w.phrase_norm])]
            productive.sort(key=lambda w: (-(w.tok_end - w.tok_start),
                                           min(METHOD_RANK.get(g["method"], 9) for g in groups_by_phrase[w.phrase_norm]),
                                           w.tok_start))
            taken: list[tuple[int, int]] = []
            for w in productive:
                if any(w.tok_start < e and w.tok_end > s for s, e in taken):
                    continue
                taken.append((w.tok_start, w.tok_end))
                self._emit(item, w, groups_by_phrase[w.phrase_norm], result)
            for w in ws:  # recognized concepts not overlapped by a productive match
                overl = any(w.tok_start < e and w.tok_end > s for s, e in taken)
                for g in groups_by_phrase[w.phrase_norm]:
                    if not g["codes"] and not overl:
                        result.concepts.append(self._concept_row(item, w, g["hit"].cui, g["method"], False))
            for w in ws:
                covered_any.add((ii, si, w.tok_start, w.tok_end))
        self._collect_unmatched(items, wins, covered_any, result)
        return result

    def _concept_row(self, item, w, cui, method, has_code):
        return {"policy_uid": item.policy_uid, "cui": cui, "evidence_source": item.evidence_source, "method": method,
                "has_code_link": has_code, "example_phrase": w.raw_text[:200], "section_seq": item.section_seq}

    def _emit(self, item: TextItem, w: Window, groups: list[dict], result: MatchResult):
        # all codes this phrase points to (across atoms) define the candidate set.
        # Deterministic credit: strongest method, then vocabulary/term type, then atom id.
        groups = sorted(groups, key=lambda g: (METHOD_RANK.get(g["method"], 9), str(g["hit"].sab),
                                               TTY_RANK.get(g["hit"].tty, 9), str(g["hit"].aui)))
        all_codes = {}
        for g in groups:
            for k, v in g["codes"].items():
                all_codes.setdefault(k, (v, g))
        size = len(all_codes)
        cap = self.cfg.max_candidates_per_phrase
        keys = sorted(all_codes)[:cap]
        truncated = size > cap
        n_tok = w.tok_end - w.tok_start
        for key in keys:
            (code_tuple, g) = all_codes[key]
            h = g["hit"]
            ratio = getattr(h, "ratio", None)
            score = evidence_score(g["method"], size, n_tok, item.section_role,
                                   suppressible=(h.suppress or "N") != "N",
                                   fuzzy_ratio=None if ratio is None or pd.isna(ratio) else ratio)
            result.evidence.append(InferredEvidence(
                policy_uid=item.policy_uid, evidence_source=item.evidence_source, method=g["method"],
                code_system=key[0], code=key[1], code_sab=code_tuple[0], section_seq=item.section_seq,
                hierarchy_path=item.hierarchy_path, matched_phrase=w.raw_text, normalized_phrase=w.phrase_norm,
                abbreviation_expanded=w.abbreviation_expanded, matched_term=h.str, matched_term_tty=h.tty,
                matched_sab=h.sab, matched_aui=h.aui, matched_srl=None if pd.isna(h.srl) else int(h.srl), cui=h.cui,
                relationship_path=g["path"], char_start=w.char_start, char_end=w.char_end, candidate_set_size=size,
                candidate_set_truncated=truncated, score=score))
        for g in groups:
            result.concepts.append(self._concept_row(item, w, g["hit"].cui, g["method"], bool(g["codes"])))
        if truncated:
            log.warning("Phrase '%s' (%s) points to %d codes; kept first %d and flagged truncated",
                        w.phrase_norm, item.policy_uid, size, cap)

    def _collect_unmatched(self, items, wins, covered, result):
        seen = set()
        for w in sorted(wins, key=lambda x: -(x.tok_end - x.tok_start)):
            n = w.tok_end - w.tok_start
            if not 2 <= n <= 4:
                continue
            words = w.phrase_norm.split()
            if not words[-1].endswith(PROCEDURE_CUES) or words[0] in UNMATCHED_LEADING_NOISE:
                continue
            if any(t in BAG_STOPWORDS and t not in ("of", "and") for t in words[1:-1]):
                continue
            if any(k[0] == w.item_idx and k[1] == w.sent_idx and w.tok_start < k[3] and w.tok_end > k[2] for k in covered):
                continue
            key = (w.item_idx, w.sent_idx)
            if any(s[0] == key and w.tok_start < s[2] and w.tok_end > s[1] for s in seen):
                continue
            seen.add((key, w.tok_start, w.tok_end))
            it = items[w.item_idx]
            result.unmatched.append({"policy_uid": it.policy_uid, "phrase_norm": w.phrase_norm,
                                     "evidence_source": it.evidence_source, "example_text": w.raw_text[:200],
                                     "section_seq": it.section_seq})


def policy_abbreviations(policy) -> dict[str, str]:
    abbr = {}
    for text in [policy.policy_title or ""] + [s.content for s in policy.sections]:
        abbr.update(find_abbreviations(text))
    return abbr
