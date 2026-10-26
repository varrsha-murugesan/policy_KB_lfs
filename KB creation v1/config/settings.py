"""Central configuration for the payer policy intelligence project.

Every tunable used by the pipeline lives here so that a processing run can be
reproduced from (code version + settings + UMLS release + input file hash).
Values can be overridden with environment variables (see .env.example).
Secrets (the UMLS API key) are NEVER stored here; they are read from the
environment at call time by src.umls.api_client.get_api_key().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load a local .env file (excluded from git)
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover - python-dotenv is in requirements
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSING_VERSION = "ppi-1.0.0"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Paths:
    root: Path = PROJECT_ROOT
    database: Path = _env_path("PPI_DATABASE", PROJECT_ROOT / "database" / "payer_policy_kb.duckdb")
    schema_sql: Path = PROJECT_ROOT / "database" / "schema.sql"
    input_json: Path = _env_path("PPI_INPUT_JSON", PROJECT_ROOT / "data" / "input" / "extraction_contents.json")
    raw_dir: Path = _env_path("PPI_RAW_DIR", PROJECT_ROOT / "data" / "raw")
    meta_dir: Path = _env_path("PPI_META_DIR", PROJECT_ROOT / "data" / "raw" / "META")
    exports_dir: Path = _env_path("PPI_EXPORTS_DIR", PROJECT_ROOT / "data" / "exports")
    review_dir: Path = PROJECT_ROOT / "data" / "review"
    cache_dir: Path = PROJECT_ROOT / "data" / "processed" / "api_cache"
    logs_dir: Path = PROJECT_ROOT / "logs"


@dataclass(frozen=True)
class UmlsSettings:
    rest_base: str = "https://uts-ws.nlm.nih.gov/rest"
    releases_url: str = "https://uts-ws.nlm.nih.gov/releases"
    download_url: str = "https://uts-ws.nlm.nih.gov/download"
    # NLM policy: max 20 requests/second/IP. We stay well below it.
    max_requests_per_second: float = _env_float("UMLS_MAX_RPS", 8.0)
    cache_ttl_hours: float = 24.0  # NLM recommends caching 12-24h
    request_timeout_s: float = 60.0
    max_retries: int = 5
    release_type: str = "umls-metathesaurus-full-subset"
    # RRF files needed from the Metathesaurus. MRSAT is optional (very large).
    required_rrf: tuple = ("MRCONSO", "MRSTY", "MRREL", "MRSAB")
    optional_rrf: tuple = ("MRDEF", "MRRANK", "MRSAT")


@dataclass(frozen=True)
class VocabularyScope:
    # Vocabularies whose CODES are mapping targets.
    #   CPT  = AMA CPT (restriction category 3)
    #   HCPT = CPT as distributed inside HCPCS by CMS (category 3)
    #   HCPCS = HCPCS Level II (category 0 in the 2026AA appendix)
    code_sabs: tuple = ("CPT", "HCPT", "HCPCS")
    # NLM-created hierarchy sources for CPT / HCPCS group headings.
    hierarchy_sabs: tuple = ("MTHCH", "MTHHH")
    # Term types that represent a billable code-level description.
    code_level_ttys: tuple = ("PT", "SY", "ETCLIN", "ETCF", "POS", "GLP", "MP", "AB", "OP", "OA")
    # Semantic types used to pull in a procedure lexicon from OTHER
    # vocabularies (SNOMED CT, MeSH, NCI, ...). TUIs from the UMLS Semantic
    # Network "Procedures" group plus Health Care Activity.
    procedure_tuis: tuple = ("T058", "T059", "T060", "T061", "T063")
    # Max source restriction level (SRL) allowed for the synonym lexicon.
    # 0-4 = license categories; SNOMED CT is SRL 9 (Appendix 2). Set to 3 to
    # exclude SNOMED, 0 for unrestricted sources only.
    max_lexicon_srl: int = int(os.environ.get("PPI_MAX_LEXICON_SRL", 9))
    # MRREL relationship labels we keep. PAR/CHD/RB/RN = hierarchy,
    # RQ = possibly synonymous, SY = source-asserted synonymy, RO = other.
    keep_rels: tuple = ("PAR", "CHD", "RB", "RN", "RQ", "SY", "RO")
    # Relationships allowed to GENERATE candidate codes (Section 17.4).
    # RO (e.g. add_on_code_for) is stored but not used to infer codes.
    candidate_rels: tuple = ("SY", "RQ", "RN", "CHD")


@dataclass(frozen=True)
class MatchingSettings:
    max_phrase_tokens: int = 8
    fuzzy_enabled: bool = True
    fuzzy_threshold: float = 92.0          # rapidfuzz token_sort_ratio, 0-100
    fuzzy_min_tokens: int = 2
    max_fuzzy_candidates_per_window: int = 3
    max_candidates_per_phrase: int = 25    # larger sets are recorded as truncated
    max_hierarchy_depth: int = 4
    single_token_min_len: int = 5
    # Corpus frequency above which a matched phrase is treated as generic.
    generic_phrase_doc_fraction: float = 0.30
    # Sentences repeated in >= this fraction of a payer's policies are boilerplate.
    boilerplate_doc_fraction: float = 0.20
    boilerplate_min_docs: int = 5
    # Inference over attachment pages (often long code tables). Explicit codes are always
    # extracted from attachments; terminology inference there is opt-in (--infer-attachments).
    infer_from_attachments: bool = False
    rules_from_attachments: bool = False
    # Static, versioned list of phrases never used as evidence (replaces per-batch suppression
    # so a policy's result never depends on which other policies are in the run).
    generic_phrases_file: Path = PROJECT_ROOT / "config" / "generic_phrases.json"
    # Policies with more distinct listed codes than this are 'code_list_policy'.
    code_list_policy_min_codes: int = 150


@dataclass(frozen=True)
class PolicyMatchSettings:
    strong_threshold: float = 55.0
    possible_threshold: float = 30.0
    weak_threshold: float = 12.0
    min_rule_similarity: float = 0.45
    # combined-score weights (sum 100); each dimension is a 0..1 IDF-weighted cosine
    w_code: float = 45.0
    w_concept: float = 25.0
    w_title: float = 15.0
    w_relatedness: float = 15.0
    dimension_threshold: float = 0.30      # a dimension 'supports' a match at or above this value
    # how much a code counts by provenance (stated in text counts most)
    provenance_weight: dict = field(default_factory=lambda: {"explicit": 1.0, "supplementary": 0.8,
                                                              "code_list": 0.6, "inferred": 0.5})
    max_block_df: int = 200                # keys shared by more policies are not used to generate candidate pairs


@dataclass(frozen=True)
class LicensingSettings:
    # Exports of restricted vocabulary STRINGS (CPT descriptors, SNOMED terms)
    # are redacted unless explicitly enabled. Codes and CUIs are identifiers
    # and are always exported. See README "Licensing".
    export_restricted_strings: bool = os.environ.get("PPI_EXPORT_RESTRICTED_STRINGS", "false").lower() == "true"
    # Sources with SRL above this are considered restricted for export.
    unrestricted_max_srl: int = 0


@dataclass(frozen=True)
class Settings:
    paths: Paths = field(default_factory=Paths)
    umls: UmlsSettings = field(default_factory=UmlsSettings)
    vocab: VocabularyScope = field(default_factory=VocabularyScope)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    policy_match: PolicyMatchSettings = field(default_factory=PolicyMatchSettings)
    licensing: LicensingSettings = field(default_factory=LicensingSettings)
    processing_version: str = PROCESSING_VERSION

    def as_dict(self) -> dict:
        """Serializable snapshot (no secrets) stored with every run."""
        from dataclasses import asdict

        import json

        return json.loads(json.dumps(asdict(self), default=str))


SETTINGS = Settings()
