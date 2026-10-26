-- ============================================================================
-- Payer Policy Intelligence - DuckDB schema (schema_version 1)
-- ============================================================================
-- Layer 1: terminology knowledge base (loaded from UMLS RRF files)
-- Layer 2: policy corpus (loaded from the extraction JSON)
-- Layer 3: derived results (code mentions, CPT mappings, rules, comparisons)
-- Layer 4: human review + run provenance
--
-- Idempotency: every derived table is rewritten per policy inside a single
-- transaction (DELETE children -> DELETE parent rows of the policy -> INSERT).
-- Natural/hashed keys make repeated runs produce identical primary keys.
--
-- Foreign keys are LOGICAL (marked "FK -> table(col) [validated]") rather than
-- declared. DuckDB checks foreign keys against the state at the start of a
-- transaction, so the pattern this project relies on for atomic, idempotent
-- re-runs - delete a policy's derived rows, update the policy, insert new rows,
-- all in ONE transaction - raises constraint errors when FKs are declared
-- (reproduced while building this project with DuckDB 1.5). Every logical FK is
-- checked by src/knowledge_base/validation.py after each build and mapping run
-- (scripts/validate_results.py reports them), so integrity is verified, not assumed.
-- ============================================================================

CREATE TABLE IF NOT EXISTS schema_meta (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

-- ---------------------------------------------------------------- runs ------
CREATE TABLE IF NOT EXISTS processing_runs (
    run_id             VARCHAR PRIMARY KEY,           -- e.g. map-20261013T101500-ab12cd
    run_type           VARCHAR NOT NULL,              -- kb_build | map | compare | review_import
    status             VARCHAR NOT NULL,              -- running | succeeded | failed
    started_at         TIMESTAMP NOT NULL,
    finished_at        TIMESTAMP,
    processing_version VARCHAR NOT NULL,
    umls_release       VARCHAR,                       -- KB release used / built
    input_file         VARCHAR,
    input_sha256       VARCHAR,
    settings_json      JSON,
    stats_json         JSON,
    error_message      VARCHAR
);

-- Resumable KB build: one row per (release, step).
CREATE TABLE IF NOT EXISTS kb_build_steps (
    umls_release VARCHAR NOT NULL,
    step         VARCHAR NOT NULL,                    -- sab | concepts | terms | ...
    status       VARCHAR NOT NULL,                    -- running | done | failed
    row_count    BIGINT,
    source_files VARCHAR,                             -- file names + sizes + mtimes
    run_id       VARCHAR,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP,
    detail       VARCHAR,
    PRIMARY KEY (umls_release, step)
);

-- ------------------------------------------------------ terminology KB -----
CREATE TABLE IF NOT EXISTS source_vocabularies (
    rsab          VARCHAR PRIMARY KEY,                -- root source abbreviation (CPT)
    vsab          VARCHAR,                            -- versioned (CPT2026)
    son           VARCHAR,                            -- source official name
    srl           INTEGER,                            -- license restriction level
    sver          VARCHAR,
    lat           VARCHAR,
    is_code_source BOOLEAN NOT NULL DEFAULT FALSE,    -- mapping target vocab
    umls_release  VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS concepts (
    cui             VARCHAR PRIMARY KEY,
    preferred_name  VARCHAR,                          -- MRCONSO TS=P,STT=PF,ISPREF=Y (English)
    preferred_sab   VARCHAR,
    is_code_bearing BOOLEAN NOT NULL,                 -- has an atom in a code vocabulary
    in_procedure_lexicon BOOLEAN NOT NULL DEFAULT FALSE,
    umls_release    VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_semantic_types (
    cui  VARCHAR NOT NULL,  -- FK -> concepts(cui) [validated]
    tui  VARCHAR NOT NULL,
    sty  VARCHAR NOT NULL,
    PRIMARY KEY (cui, tui)
);

CREATE TABLE IF NOT EXISTS concept_definitions (
    atui  VARCHAR PRIMARY KEY,
    cui   VARCHAR NOT NULL,  -- FK -> concepts(cui) [validated]
    sab   VARCHAR NOT NULL,
    definition VARCHAR NOT NULL,
    suppress VARCHAR
);

-- One row per UMLS atom (AUI). Preserves original string + identifiers.
CREATE TABLE IF NOT EXISTS terms (
    aui        VARCHAR PRIMARY KEY,
    cui        VARCHAR NOT NULL,  -- FK -> concepts(cui) [validated]
    sab        VARCHAR NOT NULL,
    code       VARCHAR,
    scui       VARCHAR,
    tty        VARCHAR NOT NULL,
    str        VARCHAR NOT NULL,                      -- original string, unaltered
    str_norm   VARCHAR NOT NULL,                      -- deterministic normalization
    str_key    VARCHAR NOT NULL,                      -- order-insensitive bag key
    head_norm  VARCHAR,                               -- leading clause of a code descriptor (code-level atoms only)
    lat        VARCHAR NOT NULL,
    ispref     VARCHAR,
    ts         VARCHAR,
    stt        VARCHAR,
    suppress   VARCHAR,                               -- N | Y | E | O
    srl        INTEGER,
    is_code_level BOOLEAN NOT NULL,                   -- atom of a code vocabulary describing a code
    umls_release VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_terms_norm ON terms(str_norm);
CREATE INDEX IF NOT EXISTS idx_terms_key  ON terms(str_key);
CREATE INDEX IF NOT EXISTS idx_terms_head ON terms(head_norm);
CREATE INDEX IF NOT EXISTS idx_terms_code ON terms(sab, code);
CREATE INDEX IF NOT EXISTS idx_terms_cui  ON terms(cui);

-- Code vocabulary entries (CPT, HCPT, HCPCS). A code may be linked to several
-- CUIs via its atoms: see view v_code_concepts.
CREATE TABLE IF NOT EXISTS cpt_codes (
    sab            VARCHAR NOT NULL,                  -- CPT | HCPT | HCPCS
    code           VARCHAR NOT NULL,
    code_system    VARCHAR NOT NULL,                  -- CPT | HCPCS_L2
    code_category  VARCHAR NOT NULL,                  -- CPT_I | CPT_II | CPT_III | CPT_PLA | CPT_MAAA | HCPCS_L2
    primary_cui    VARCHAR,  -- FK -> concepts(cui) [validated]
    description    VARCHAR,                           -- PT string from source (restricted for CPT)
    description_tty VARCHAR,
    full_description VARCHAR,                         -- MRSAT CPF when loaded
    is_suppressed  BOOLEAN NOT NULL,
    umls_release   VARCHAR NOT NULL,
    PRIMARY KEY (sab, code)
);
CREATE INDEX IF NOT EXISTS idx_cpt_codes_code ON cpt_codes(code);

CREATE TABLE IF NOT EXISTS concept_relationships (
    rui    VARCHAR PRIMARY KEY,
    cui1   VARCHAR NOT NULL,
    rel    VARCHAR NOT NULL,                          -- MRREL semantics: CUI2 has REL to CUI1
    cui2   VARCHAR NOT NULL,
    rela   VARCHAR,
    sab    VARCHAR NOT NULL,
    dir    VARCHAR,
    suppress VARCHAR,
    umls_release VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rel_cui1 ON concept_relationships(cui1);
CREATE INDEX IF NOT EXISTS idx_rel_cui2 ON concept_relationships(cui2);

CREATE OR REPLACE VIEW v_code_concepts AS
SELECT DISTINCT t.sab, t.code, t.cui
FROM terms t
JOIN cpt_codes c ON c.sab = t.sab AND c.code = t.code
WHERE t.is_code_level;

-- ------------------------------------------------------------- policies -----
CREATE TABLE IF NOT EXISTS policies (
    policy_uid       VARCHAR PRIMARY KEY,             -- payer slug + hash(source_pdf | number+title)
    payer            VARCHAR NOT NULL,
    payer_source     VARCHAR NOT NULL,                -- cli | detected | field | unknown
    policy_number    VARCHAR,                         -- NOT unique in real data
    policy_title     VARCHAR,
    effective_date   VARCHAR,
    line_of_business VARCHAR,
    billing_class    VARCHAR,
    source_pdf       VARCHAR,
    content_sha256   VARCHAR NOT NULL,
    n_sections       INTEGER NOT NULL,
    quality_flags    VARCHAR[],
    raw_metadata     JSON,
    last_run_id      VARCHAR,
    updated_at       TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_uid     VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    content_sha256 VARCHAR NOT NULL,
    effective_date VARCHAR,
    first_seen_run VARCHAR NOT NULL,
    first_seen_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (policy_uid, content_sha256)
);

CREATE TABLE IF NOT EXISTS policy_sections (
    policy_uid     VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    section_seq    INTEGER NOT NULL,                  -- order in extracted_content
    title          VARCHAR,
    section        VARCHAR,
    subsection     VARCHAR,
    hierarchy_path VARCHAR,
    page_start     INTEGER,
    page_end       INTEGER,
    content_type   VARCHAR,                           -- paragraph | bullet_point | definition | table_row
    section_role   VARCHAR,                           -- overview | payment_policy | definition | reference | other
    scope_label    VARCHAR,                           -- e.g. "Kentucky Medicaid" parsed from section
    content        VARCHAR,                           -- original text, unaltered
    upstream_codes VARCHAR[],                         -- 'codes' field as delivered (unvalidated)
    table_data     JSON,
    PRIMARY KEY (policy_uid, section_seq)
);

-- Every explicit code-like token found in a policy (CPT, HCPCS, modifiers,
-- revenue codes, ICD-10-CM, DRG, unclassified upstream tokens).
CREATE TABLE IF NOT EXISTS code_mentions (
    mention_id        VARCHAR PRIMARY KEY,
    policy_uid        VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    section_seq       INTEGER,                        -- NULL = title
    code_system       VARCHAR NOT NULL,               -- CPT | HCPCS_L2 | MODIFIER | REVENUE | ICD10CM | DRG | UNCLASSIFIED
    code              VARCHAR NOT NULL,
    raw_text          VARCHAR,
    char_start        INTEGER,
    char_end          INTEGER,
    context_snippet   VARCHAR,
    detection_rule    VARCHAR NOT NULL,
    validation_status VARCHAR NOT NULL,               -- validated_in_kb | not_found_in_kb | no_reference_loaded | not_validated_system
    from_range        VARCHAR,                        -- "98940-98942" when expanded from a range
    run_id            VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_policy ON code_mentions(policy_uid);

CREATE TABLE IF NOT EXISTS policy_cpt_mappings (
    mapping_id         VARCHAR PRIMARY KEY,           -- hash(policy_uid, code_system, code)
    policy_uid         VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    code_system        VARCHAR NOT NULL,              -- CPT | HCPCS_L2
    code               VARCHAR NOT NULL,
    code_sab           VARCHAR,                       -- CPT | HCPT | HCPCS (where validated)
    mapping_origin     VARCHAR NOT NULL,              -- explicit | inferred | explicit+inferred | reviewer_added
    evidence_sources   VARCHAR[] NOT NULL,            -- policy_content | policy_title | policy_table | reviewer
    methods            VARCHAR[] NOT NULL,
    best_method        VARCHAR NOT NULL,
    confidence_tier    VARCHAR NOT NULL,              -- A..E (see src/mapping/confidence.py)
    score              DOUBLE NOT NULL,               -- ranking score, NOT a probability
    cuis               VARCHAR[],
    scope              VARCHAR NOT NULL,              -- policy_level | section_level
    section_seqs       INTEGER[],
    is_ambiguous       BOOLEAN NOT NULL,
    has_conflict       BOOLEAN NOT NULL,
    flags              VARCHAR[],
    needs_review       BOOLEAN NOT NULL,
    review_status      VARCHAR NOT NULL,              -- unreviewed | approved | rejected | corrected
    umls_release       VARCHAR,
    processing_version VARCHAR NOT NULL,
    run_id             VARCHAR NOT NULL,
    UNIQUE (policy_uid, code_system, code)
);

CREATE TABLE IF NOT EXISTS mapping_evidence (
    evidence_id        VARCHAR PRIMARY KEY,
    mapping_id         VARCHAR NOT NULL,              -- -> policy_cpt_mappings (FK enforced by validation; see header)
    policy_uid         VARCHAR NOT NULL,
    evidence_source    VARCHAR NOT NULL,              -- policy_content | policy_title | policy_table | reviewer
    method             VARCHAR NOT NULL,              -- explicit_code | exact_preferred_term | exact_synonym | ...
    section_seq        INTEGER,
    hierarchy_path     VARCHAR,
    matched_phrase     VARCHAR,                       -- text as it appears in the policy
    normalized_phrase  VARCHAR,
    abbreviation_expanded VARCHAR,                    -- e.g. "clbp -> chronic lower back pain"
    matched_term       VARCHAR,                       -- KB string (restricted for CPT/SNOMED)
    matched_term_tty   VARCHAR,
    matched_sab        VARCHAR,
    matched_aui        VARCHAR,
    cui                VARCHAR,
    relationship_path  VARCHAR,                       -- e.g. "C1 -RN-> C2"
    mention_id         VARCHAR,                       -- for explicit codes
    char_start         INTEGER,
    char_end           INTEGER,
    candidate_set_size INTEGER,                       -- how many codes this phrase pointed to
    candidate_set_truncated BOOLEAN DEFAULT FALSE,
    score              DOUBLE NOT NULL,
    rule_id            VARCHAR NOT NULL,              -- versioned rule identifier
    run_id             VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_mapping ON mapping_evidence(mapping_id);

-- UMLS concepts recognized in a policy (with or without a code link).
-- Used for concept-based policy matching and clustering.
CREATE TABLE IF NOT EXISTS policy_concepts (
    policy_uid      VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    cui             VARCHAR NOT NULL,
    evidence_source VARCHAR NOT NULL,                 -- policy_title | policy_content
    method          VARCHAR NOT NULL,
    has_code_link   BOOLEAN NOT NULL,
    example_phrase  VARCHAR,
    occurrences     INTEGER NOT NULL,
    section_seqs    INTEGER[],
    run_id          VARCHAR NOT NULL,
    PRIMARY KEY (policy_uid, cui, evidence_source)
);

-- Phrases that looked like procedures but matched nothing (coverage gaps).
CREATE TABLE IF NOT EXISTS unmatched_terms (
    policy_uid     VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    phrase_norm    VARCHAR NOT NULL,
    evidence_source VARCHAR NOT NULL,
    example_text   VARCHAR,
    occurrences    INTEGER NOT NULL,
    run_id         VARCHAR NOT NULL,
    PRIMARY KEY (policy_uid, phrase_norm, evidence_source)
);

-- Human decisions. Scoped to ONE policy: never applied globally.
CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id    VARCHAR PRIMARY KEY,
    policy_uid     VARCHAR NOT NULL,
    code_system    VARCHAR NOT NULL,
    code           VARCHAR NOT NULL,
    decision       VARCHAR NOT NULL,                  -- approve | reject | add
    reviewer       VARCHAR NOT NULL,
    reason         VARCHAR,
    decided_at     TIMESTAMP NOT NULL,
    evidence_fingerprint VARCHAR,                     -- evidence the reviewer saw
    umls_release   VARCHAR,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE      -- newer decision supersedes older
);
CREATE INDEX IF NOT EXISTS idx_review_policy ON review_decisions(policy_uid, code_system, code);

-- ------------------------------------------------------------ rules --------
CREATE TABLE IF NOT EXISTS policy_rules (
    rule_id         VARCHAR PRIMARY KEY,
    policy_uid      VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    section_seq     INTEGER NOT NULL,
    sentence_idx    INTEGER NOT NULL,
    rule_type       VARCHAR NOT NULL,
    polarity        VARCHAR NOT NULL,                 -- require | not_required | allow | deny | limit | inform
    scope_label     VARCHAR,
    rule_text       VARCHAR NOT NULL,
    normalized_text VARCHAR NOT NULL,
    attributes      JSON,                              -- codes, modifiers, numbers, percents, settings...
    is_boilerplate  BOOLEAN NOT NULL,
    extraction_rule VARCHAR NOT NULL,
    run_id          VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_policy ON policy_rules(policy_uid);

-- ------------------------------------------------------- comparisons -------
CREATE TABLE IF NOT EXISTS policy_pair_matches (
    pair_id          VARCHAR PRIMARY KEY,
    policy_uid_a     VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    policy_uid_b     VARCHAR NOT NULL,  -- FK -> policies(policy_uid) [validated]
    payer_a          VARCHAR NOT NULL,
    payer_b          VARCHAR NOT NULL,
    score            DOUBLE NOT NULL,
    match_tier       VARCHAR NOT NULL,                -- strong | possible | weak
    scope_alignment  VARCHAR NOT NULL,                -- aligned | differs | unknown
    score_breakdown  JSON NOT NULL,
    shared_explicit_codes VARCHAR[],
    shared_inferred_codes VARCHAR[],
    shared_cuis      VARCHAR[],
    scope_differences JSON,
    explanation      VARCHAR NOT NULL,
    run_id           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_gap_findings (
    finding_id    VARCHAR PRIMARY KEY,
    pair_id       VARCHAR NOT NULL,                   -- -> policy_pair_matches (FK enforced by validation)
    rule_type     VARCHAR NOT NULL,
    finding_type  VARCHAR NOT NULL,                   -- see src/comparison/rule_comparator.py
    rule_id_a     VARCHAR,
    rule_id_b     VARCHAR,
    similarity    DOUBLE,
    detail        JSON,
    status        VARCHAR NOT NULL,                   -- needs_review | informational
    run_id        VARCHAR NOT NULL
);

-- ============================================================================
-- schema_version 2: payer-agnostic ingestion, code provenance, enrichment,
-- multi-dimensional matching. Additive and idempotent: existing v1 databases
-- are upgraded in place by re-running initialize_database.py.
-- ============================================================================
CREATE TABLE IF NOT EXISTS source_documents (
    source_document_id VARCHAR PRIMARY KEY,           -- sha256 of the file bytes (first 20 hex)
    payer              VARCHAR NOT NULL,
    file_name          VARCHAR NOT NULL,
    file_sha256        VARCHAR NOT NULL,
    adapter            VARCHAR NOT NULL,              -- adapter name + version
    payer_profile      VARCHAR,                        -- profile file used
    n_records          INTEGER NOT NULL,
    schema_report      JSON,                           -- keys found, record statuses, warnings
    loaded_run_id      VARCHAR NOT NULL,
    loaded_at          TIMESTAMP DEFAULT current_timestamp
);

ALTER TABLE policies ADD COLUMN IF NOT EXISTS source_document_id VARCHAR;
ALTER TABLE policies ADD COLUMN IF NOT EXISTS source_pointer VARCHAR;          -- JSON path of the record, e.g. $[12]
ALTER TABLE policies ADD COLUMN IF NOT EXISTS adapter VARCHAR;
ALTER TABLE policies ADD COLUMN IF NOT EXISTS record_status VARCHAR;           -- ok | extraction_failure
ALTER TABLE policies ADD COLUMN IF NOT EXISTS title_quality VARCHAR;           -- specific | generic | missing
ALTER TABLE policies ADD COLUMN IF NOT EXISTS identifier_quality VARCHAR;      -- source_id | content_hash | content_hash_duplicate
ALTER TABLE policies ADD COLUMN IF NOT EXISTS policy_class VARCHAR;            -- standard | code_list_policy | administrative
ALTER TABLE policies ADD COLUMN IF NOT EXISTS n_distinct_codes INTEGER;

ALTER TABLE policy_sections ADD COLUMN IF NOT EXISTS source_pointer VARCHAR;   -- JSON path of the element
ALTER TABLE policy_sections ADD COLUMN IF NOT EXISTS evidence_location VARCHAR;-- policy_text | policy_table | policy_qa | attachment
ALTER TABLE policy_sections ADD COLUMN IF NOT EXISTS attachment_url VARCHAR;

ALTER TABLE code_mentions ADD COLUMN IF NOT EXISTS code_location VARCHAR;      -- title | policy_text | policy_table | policy_qa | attachment | code_list_field
ALTER TABLE code_mentions ADD COLUMN IF NOT EXISTS source_pointer VARCHAR;
ALTER TABLE code_mentions ADD COLUMN IF NOT EXISTS quality_flags VARCHAR[];

ALTER TABLE policy_cpt_mappings ADD COLUMN IF NOT EXISTS code_provenance VARCHAR;   -- strongest location (see mapping_consolidator)
ALTER TABLE policy_cpt_mappings ADD COLUMN IF NOT EXISTS provenance_tiers VARCHAR[];

ALTER TABLE mapping_evidence ADD COLUMN IF NOT EXISTS code_location VARCHAR;
ALTER TABLE mapping_evidence ADD COLUMN IF NOT EXISTS semantic_types VARCHAR[];

ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS code_similarity DOUBLE;
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS concept_similarity DOUBLE;
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS relatedness_similarity DOUBLE;
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS title_similarity DOUBLE;
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS relation_class VARCHAR;    -- code_overlap | concept_overlap | clinically_related | title_only
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS needs_review BOOLEAN;
ALTER TABLE policy_pair_matches ADD COLUMN IF NOT EXISTS review_reasons VARCHAR[];

-- Per-code UMLS enrichment derived from the loaded KB (rebuilt per release).
CREATE TABLE IF NOT EXISTS code_enrichment (
    code_system     VARCHAR NOT NULL,
    code            VARCHAR NOT NULL,
    sab             VARCHAR NOT NULL,
    cuis            VARCHAR[],
    tuis            VARCHAR[],
    semantic_types  VARCHAR[],
    family_cui      VARCHAR,                          -- nearest hierarchy ancestor (service group)
    family_name     VARCHAR,
    family_size     INTEGER,                          -- codes under the family
    family_source   VARCHAR,                          -- relationship SAB used, or 'none_available'
    umls_release    VARCHAR,
    PRIMARY KEY (code_system, code)
);

-- Per section: what kind of code evidence it carries (or why none).
CREATE TABLE IF NOT EXISTS section_mapping_status (
    policy_uid   VARCHAR NOT NULL,                    -- FK -> policies [validated]
    section_seq  INTEGER NOT NULL,
    status       VARCHAR NOT NULL,                    -- see src/mapping/section_status.py
    reasons      VARCHAR[],
    run_id       VARCHAR NOT NULL,
    PRIMARY KEY (policy_uid, section_seq)
);

-- Service groups (code families) per policy, from high-confidence mappings.
CREATE TABLE IF NOT EXISTS policy_service_groups (
    policy_uid   VARCHAR NOT NULL,
    family_cui   VARCHAR NOT NULL,
    family_name  VARCHAR,
    n_codes      INTEGER NOT NULL,
    provenance   VARCHAR[],
    run_id       VARCHAR NOT NULL,
    PRIMARY KEY (policy_uid, family_cui)
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();
