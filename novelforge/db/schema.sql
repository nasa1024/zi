-- NovelForge novel.db schema (MVP0 subset of design §02).
-- Single source of truth = SQLite. markdown is read-only render output.
-- scene_vec (sqlite-vec / vec0) is intentionally NOT created here (MVP2; needs extension).
-- Build order follows §2.10 (referenced tables first). FK enforcement is per-row at DML time,
-- so forward references in CREATE are fine; PRAGMAs are set by connection.py (not here).

-- 1) global metadata / version binding ----------------------------------------
CREATE TABLE IF NOT EXISTS meta_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2) entities + aliases -------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL UNIQUE,
    entity_type     TEXT NOT NULL
                        CHECK(entity_type IN ('character','location','item','faction','concept')),
    first_appear_chapter INTEGER,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','dead','retired','sealed')),
    detail_json     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(detail_json IS NULL OR json_valid(detail_json))
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL,
    alias           TEXT NOT NULL,
    alias_type      TEXT NOT NULL DEFAULT 'nickname'
                        CHECK(alias_type IN ('nickname','formal','title','old_name','typo')),
    valid_from_chapter INTEGER,
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    UNIQUE(alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON entity_aliases(entity_id);

-- 3) power_ranks + character_power_log ----------------------------------------
CREATE TABLE IF NOT EXISTS power_ranks (
    id              TEXT PRIMARY KEY,
    system_name     TEXT NOT NULL,
    rank_name       TEXT NOT NULL,
    rank_order      INTEGER NOT NULL,
    detail_json     TEXT,
    UNIQUE(system_name, rank_name),
    UNIQUE(system_name, rank_order)
);
CREATE INDEX IF NOT EXISTS idx_prank_order ON power_ranks(system_name, rank_order);

CREATE TABLE IF NOT EXISTS character_power_log (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL,
    system_name     TEXT NOT NULL,
    rank_id         TEXT NOT NULL,
    rank_order      INTEGER NOT NULL,
    change_chapter  INTEGER NOT NULL,
    change_type     TEXT NOT NULL
                        CHECK(change_type IN ('breakthrough','injury_drop','seal','unseal','init')),
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(rank_id)   REFERENCES power_ranks(id)
);
CREATE INDEX IF NOT EXISTS idx_cpl_entity ON character_power_log(entity_id, change_chapter);

-- 4) geo + travel + timeline --------------------------------------------------
CREATE TABLE IF NOT EXISTS geo_locations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    region          TEXT,
    parent_location_id TEXT,
    coord_x         REAL,
    coord_y         REAL,
    detail_json     TEXT,
    FOREIGN KEY(parent_location_id) REFERENCES geo_locations(id),
    CHECK(detail_json IS NULL OR json_valid(detail_json))
);

CREATE TABLE IF NOT EXISTS travel_edges (
    id              TEXT PRIMARY KEY,
    from_location_id TEXT NOT NULL,
    to_location_id   TEXT NOT NULL,
    travel_mode      TEXT NOT NULL DEFAULT 'walk'
                        CHECK(travel_mode IN ('walk','horse','flight','teleport','vehicle')),
    travel_cost      INTEGER NOT NULL,
    time_unit        TEXT NOT NULL DEFAULT 'minute',
    constraint_json  TEXT,
    FOREIGN KEY(from_location_id) REFERENCES geo_locations(id),
    FOREIGN KEY(to_location_id)   REFERENCES geo_locations(id),
    UNIQUE(from_location_id, to_location_id, travel_mode),
    CHECK(constraint_json IS NULL OR json_valid(constraint_json))
);
CREATE INDEX IF NOT EXISTS idx_travel_from ON travel_edges(from_location_id);

CREATE TABLE IF NOT EXISTS timeline_events (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    chapter         INTEGER NOT NULL,
    story_time_start INTEGER NOT NULL,
    story_time_end   INTEGER NOT NULL,
    time_unit       TEXT NOT NULL DEFAULT 'minute',
    location_id     TEXT,
    participants    TEXT,
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(location_id) REFERENCES geo_locations(id),
    CHECK(story_time_end >= story_time_start),
    CHECK(participants IS NULL OR json_valid(participants))
);
CREATE INDEX IF NOT EXISTS idx_tl_time    ON timeline_events(story_time_start, story_time_end);
CREATE INDEX IF NOT EXISTS idx_tl_chapter ON timeline_events(chapter);

-- 5) canon ledger: facts + fact_revisions ------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT,
    fact_type       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    detail_json     TEXT,
    status          TEXT NOT NULL DEFAULT 'tentative'
                        CHECK(status IN ('canon','tentative','retconned')),
    valid_from_chapter INTEGER NOT NULL,
    valid_to_chapter   INTEGER,
    current_revision_id TEXT NOT NULL,
    confidence      REAL,
    risk_tier       TEXT NOT NULL DEFAULT 'low'
                        CHECK(risk_tier IN ('low','medium','high')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    CHECK(detail_json IS NULL OR json_valid(detail_json)),
    CHECK(fact_type IN (
        'world_rule','power_system','character_trait','relationship',
        'knowledge','event','item','location','numeric','style','misc'))
);
CREATE INDEX IF NOT EXISTS idx_facts_entity     ON facts(entity_id);
CREATE INDEX IF NOT EXISTS idx_facts_status     ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_validrange ON facts(valid_from_chapter, valid_to_chapter);
CREATE INDEX IF NOT EXISTS idx_facts_subj_pred  ON facts(subject, predicate);
CREATE INDEX IF NOT EXISTS idx_facts_type       ON facts(fact_type);

CREATE TABLE IF NOT EXISTS fact_revisions (
    id              TEXT PRIMARY KEY,
    fact_id         TEXT NOT NULL,
    revision_no     INTEGER NOT NULL,
    op              TEXT NOT NULL
                        CHECK(op IN ('add','update','deprecate','retcon','revert')),
    old_object      TEXT,
    new_object      TEXT,
    old_status      TEXT,
    new_status      TEXT NOT NULL,
    valid_from_chapter INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    evidence_refs   TEXT,
    actor           TEXT NOT NULL,
    policy_mode     TEXT,
    source_candidate_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(fact_id) REFERENCES facts(id),
    UNIQUE(fact_id, revision_no),
    CHECK(evidence_refs IS NULL OR json_valid(evidence_refs))
);
CREATE INDEX IF NOT EXISTS idx_factrev_fact ON fact_revisions(fact_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_factrev_op   ON fact_revisions(op);

-- append-only guards for fact_revisions
CREATE TRIGGER IF NOT EXISTS trg_factrev_no_update
    BEFORE UPDATE ON fact_revisions
    BEGIN SELECT RAISE(ABORT, 'fact_revisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_factrev_no_delete
    BEFORE DELETE ON fact_revisions
    BEGIN SELECT RAISE(ABORT, 'fact_revisions is append-only'); END;

-- 6) knowledge / item / gimmick / numeric ------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_edges (
    id              TEXT PRIMARY KEY,
    knower_entity_id TEXT NOT NULL,
    secret_key      TEXT NOT NULL,
    secret_fact_id  TEXT,
    knowledge_state TEXT NOT NULL
                        CHECK(knowledge_state IN ('knows','suspects','unaware','misinformed')),
    learned_chapter INTEGER NOT NULL,
    source          TEXT,
    public_from_chapter INTEGER,
    secrecy_level   TEXT
                        CHECK(secrecy_level IS NULL OR secrecy_level IN ('public','open_secret','secret','top_secret')),
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(knower_entity_id) REFERENCES entities(id),
    UNIQUE(knower_entity_id, secret_key, learned_chapter)
);
CREATE INDEX IF NOT EXISTS idx_know_knower ON knowledge_edges(knower_entity_id, secret_key, learned_chapter);
CREATE INDEX IF NOT EXISTS idx_know_secret ON knowledge_edges(secret_key);
CREATE INDEX IF NOT EXISTS idx_know_public ON knowledge_edges(secret_key, public_from_chapter);

CREATE TABLE IF NOT EXISTS item_ownership (
    id              TEXT PRIMARY KEY,
    item_entity_id  TEXT NOT NULL,
    owner_entity_id TEXT,
    quantity        INTEGER NOT NULL DEFAULT 1,
    since_chapter   INTEGER NOT NULL,
    current_log_id  TEXT,
    FOREIGN KEY(item_entity_id)  REFERENCES entities(id),
    FOREIGN KEY(owner_entity_id) REFERENCES entities(id),
    UNIQUE(item_entity_id)
);
CREATE INDEX IF NOT EXISTS idx_itemown_owner ON item_ownership(owner_entity_id);

CREATE TABLE IF NOT EXISTS item_log (
    id              TEXT PRIMARY KEY,
    item_entity_id  TEXT NOT NULL,
    from_owner_id   TEXT,
    to_owner_id     TEXT,
    quantity_delta  INTEGER NOT NULL,
    change_chapter  INTEGER NOT NULL,
    change_type     TEXT NOT NULL
                        CHECK(change_type IN ('acquire','transfer','consume','destroy','craft','lose')),
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(item_entity_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS idx_itemlog_item ON item_log(item_entity_id, change_chapter);

CREATE TABLE IF NOT EXISTS gimmick_rules (
    id              TEXT PRIMARY KEY,
    gimmick_name    TEXT NOT NULL UNIQUE,
    owner_entity_id TEXT,
    activation_cond TEXT,
    cost_json       TEXT,
    cooldown_chapters INTEGER,
    cooldown_story_time INTEGER,
    constraint_json TEXT,
    valid_from_chapter INTEGER NOT NULL,
    fact_id         TEXT,
    FOREIGN KEY(owner_entity_id) REFERENCES entities(id),
    CHECK(cost_json IS NULL OR json_valid(cost_json)),
    CHECK(constraint_json IS NULL OR json_valid(constraint_json))
);

CREATE TABLE IF NOT EXISTS gimmick_usage_log (
    id              TEXT PRIMARY KEY,
    gimmick_id      TEXT NOT NULL,
    user_entity_id  TEXT NOT NULL,
    use_chapter     INTEGER NOT NULL,
    use_story_time  INTEGER,
    outcome         TEXT,
    paid_cost_json  TEXT,
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(gimmick_id)     REFERENCES gimmick_rules(id),
    FOREIGN KEY(user_entity_id) REFERENCES entities(id),
    CHECK(paid_cost_json IS NULL OR json_valid(paid_cost_json))
);
CREATE INDEX IF NOT EXISTS idx_gimuse_gimmick ON gimmick_usage_log(gimmick_id, use_chapter);

CREATE TABLE IF NOT EXISTS numeric_facts (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT,
    metric_key      TEXT NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT NOT NULL,
    delta_from      REAL,
    as_of_chapter   INTEGER NOT NULL,
    as_of_story_time INTEGER,
    monotonic       TEXT NOT NULL DEFAULT 'none'
                        CHECK(monotonic IN ('none','non_decreasing','non_increasing')),
    fact_id         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS idx_numf_entity ON numeric_facts(entity_id, metric_key, as_of_chapter);

-- 7) craft layer: foreshadow + beats + cards + pacing ------------------------
CREATE TABLE IF NOT EXISTS foreshadow (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    description     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'planted'
                        CHECK(state IN ('planted','reinforced','misled','paid_off','overdue')),
    planted_chapter INTEGER NOT NULL,
    due_chapter     INTEGER,
    paid_off_chapter INTEGER,
    related_entity_id TEXT,
    importance      INTEGER NOT NULL DEFAULT 3,
    fact_id         TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(related_entity_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS idx_fs_state ON foreshadow(state);
CREATE INDEX IF NOT EXISTS idx_fs_due   ON foreshadow(due_chapter);

CREATE TABLE IF NOT EXISTS beats (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    beat_type       TEXT NOT NULL
                        CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point','hook')),
    summary         TEXT NOT NULL,
    value_start     TEXT,
    value_end       TEXT,
    tension_level   INTEGER,
    related_foreshadow_id TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','drafted','satisfied','dropped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, seq),
    FOREIGN KEY(related_foreshadow_id) REFERENCES foreshadow(id)
);
CREATE INDEX IF NOT EXISTS idx_beats_chapter ON beats(chapter, seq);
CREATE INDEX IF NOT EXISTS idx_beats_type    ON beats(beat_type);

CREATE TABLE IF NOT EXISTS chapter_cards (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL UNIQUE,
    title           TEXT,
    pov_entity_id   TEXT,
    goal            TEXT,
    summary         TEXT,
    word_count      INTEGER,
    hook_text       TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','drafted','reviewed','committed')),
    draft_id        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(pov_entity_id) REFERENCES entities(id)
);

CREATE TABLE IF NOT EXISTS character_cards (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL UNIQUE,
    voice_profile   TEXT,
    arc_stages      TEXT,
    motivation      TEXT,
    relationships   TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    CHECK(voice_profile IS NULL OR json_valid(voice_profile)),
    CHECK(arc_stages   IS NULL OR json_valid(arc_stages))
);

CREATE TABLE IF NOT EXISTS pacing_state (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL UNIQUE,
    tension_level   INTEGER NOT NULL,
    pace            TEXT
                        CHECK(pace IS NULL OR pace IN ('slow','steady','fast','climax')),
    value_shift_net TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pacing_chapter ON pacing_state(chapter);

-- 8) L0 draft index + L1/L2 source tables ------------------------------------
CREATE TABLE IF NOT EXISTS draft_index (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    revision_round  INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    word_count      INTEGER,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','checked','revised','committed','archived')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, revision_round)
);
CREATE INDEX IF NOT EXISTS idx_draft_chapter ON draft_index(chapter, revision_round);

CREATE TABLE IF NOT EXISTS l1_atoms (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    draft_id        TEXT,
    atom_text       TEXT NOT NULL,
    anchor          TEXT,
    extracted_by    TEXT,
    candidate_id    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(draft_id) REFERENCES draft_index(id)
);
CREATE INDEX IF NOT EXISTS idx_l1_chapter ON l1_atoms(chapter);

CREATE TABLE IF NOT EXISTS l2_scenes (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    scene_seq       INTEGER NOT NULL,
    scene_text      TEXT NOT NULL,
    summary         TEXT,
    embedding_model   TEXT,
    embedding_dim     INTEGER,
    embedding_version TEXT,
    indexed_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, scene_seq)
);
CREATE INDEX IF NOT EXISTS idx_l2_chapter ON l2_scenes(chapter, scene_seq);

-- 9) governance: staging + audit + review queue (tables created now; flow=MVP1)
CREATE TABLE IF NOT EXISTS fact_candidates (
    candidate_id    TEXT PRIMARY KEY,
    proposal_json   TEXT NOT NULL,
    op              TEXT NOT NULL
                        CHECK(op IN ('add','update','deprecate','retcon')),
    target_fact_id  TEXT,
    fact_type       TEXT NOT NULL,
    entity_id       TEXT,
    proposed_object TEXT,
    risk_tier       TEXT NOT NULL DEFAULT 'low'
                        CHECK(risk_tier IN ('low','medium','high')),
    confidence      REAL,
    evidence_strength REAL,
    evidence_refs   TEXT,
    conflict_flags  TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed'
                        CHECK(status IN ('proposed','pending_review','promoted','rejected','superseded')),
    source_chapter  INTEGER,
    source_skill    TEXT,
    decided_by      TEXT,
    decided_at      TEXT,
    committed_revision_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(target_fact_id) REFERENCES facts(id),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    CHECK(proposal_json IS NULL OR json_valid(proposal_json)),
    CHECK(evidence_refs IS NULL OR json_valid(evidence_refs)),
    CHECK(conflict_flags IS NULL OR json_valid(conflict_flags))
);
CREATE INDEX IF NOT EXISTS idx_cand_status  ON fact_candidates(status);
CREATE INDEX IF NOT EXISTS idx_cand_risk    ON fact_candidates(risk_tier);
CREATE INDEX IF NOT EXISTS idx_cand_chapter ON fact_candidates(source_chapter);
CREATE INDEX IF NOT EXISTS idx_cand_rank    ON fact_candidates(status, evidence_strength DESC);

CREATE TABLE IF NOT EXISTS promotion_log (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    fact_id         TEXT,
    entity_id       TEXT,
    decision        TEXT NOT NULL
                        CHECK(decision IN ('commit_canon','enqueue_review','hold_staging','reject','revert')),
    policy_mode     TEXT NOT NULL
                        CHECK(policy_mode IN ('human_gate','auto_promote','hybrid')),
    risk_tier       TEXT NOT NULL,
    evidence_strength REAL,
    chapter         INTEGER,
    conflict_summary TEXT,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    reverts_log_id  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(candidate_id)   REFERENCES fact_candidates(candidate_id),
    FOREIGN KEY(reverts_log_id) REFERENCES promotion_log(id)
);
CREATE INDEX IF NOT EXISTS idx_plog_candidate ON promotion_log(candidate_id);
CREATE INDEX IF NOT EXISTS idx_plog_fact      ON promotion_log(fact_id);
CREATE INDEX IF NOT EXISTS idx_plog_time      ON promotion_log(created_at);
CREATE TRIGGER IF NOT EXISTS trg_plog_no_update
    BEFORE UPDATE ON promotion_log
    BEGIN SELECT RAISE(ABORT, 'promotion_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_plog_no_delete
    BEFORE DELETE ON promotion_log
    BEGIN SELECT RAISE(ABORT, 'promotion_log is append-only'); END;

CREATE TABLE IF NOT EXISTS review_queue (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    risk_tier       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_review','approved','rejected','expired')),
    assigned_to     TEXT,
    enqueued_at     TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    FOREIGN KEY(candidate_id) REFERENCES fact_candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_rq_status    ON review_queue(status, priority);
CREATE INDEX IF NOT EXISTS idx_rq_candidate ON review_queue(candidate_id);

-- 10) consistency engine: author exemptions (§4.5) ---------------------------
CREATE TABLE IF NOT EXISTS consistency_exemptions (
    id              INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL,
    scope_ref       TEXT NOT NULL,
    exempt_tag      TEXT NOT NULL,
    rule_codes      TEXT,
    reason          TEXT NOT NULL,
    valid_from_chapter INTEGER,
    valid_to_chapter   INTEGER,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_exempt_scope ON consistency_exemptions(scope, scope_ref);

-- 11) derived FTS indexes (rebuildable; NOT source of truth) ------------------
-- facts_fts indexes ONLY status='canon' facts (jieba pre-tokenized in app layer).
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id    UNINDEXED,
    subject_tok,
    predicate_tok,
    object_tok,
    detail_tok,
    tokenize = "unicode61 remove_diacritics 2"
);

-- drafts_fts indexes L0 draft body lines (for "find similar passage").
CREATE VIRTUAL TABLE IF NOT EXISTS drafts_fts USING fts5(
    draft_id    UNINDEXED,
    chapter_no  UNINDEXED,
    line_start  UNINDEXED,
    line_end    UNINDEXED,
    body,
    tokenize = "unicode61 remove_diacritics 2"
);
