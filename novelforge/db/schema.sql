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
    source_fact_id  TEXT,                          -- §10 R12/A4: canon-fact origin (retcon cascade / reproject)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(rank_id)   REFERENCES power_ranks(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id)
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
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(location_id) REFERENCES geo_locations(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id),
    CHECK(story_time_end >= story_time_start),
    CHECK(participants IS NULL OR json_valid(participants))
);
CREATE INDEX IF NOT EXISTS idx_tl_time    ON timeline_events(story_time_start, story_time_end);
CREATE INDEX IF NOT EXISTS idx_tl_chapter ON timeline_events(chapter);
CREATE INDEX IF NOT EXISTS idx_tl_srcfact ON timeline_events(source_fact_id);

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
    version         INTEGER NOT NULL DEFAULT 0,    -- §10 R12/B5: optimistic-lock cursor (§11.7)
    injection_mode  TEXT NOT NULL DEFAULT 'detected'
                        CHECK(injection_mode IN ('always','detected','never')),  -- §03.8 / R14
    volume_no       INTEGER,                        -- §9.4 多卷归属（NULL=跨卷全局）
    branch_id       TEXT,                           -- §9.4 分支隔离（NULL=主线）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(branch_id) REFERENCES branches(id),
    CHECK(detail_json IS NULL OR json_valid(detail_json)),
    CHECK(fact_type IN (
        'world_rule','power_system','character_trait','relationship',
        'knowledge','event','item','location','numeric','style','misc',
        'constraint'))                              -- §10 R14/G3: always-on global taboo
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
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(knower_entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id),
    UNIQUE(knower_entity_id, secret_key, learned_chapter)
);
CREATE INDEX IF NOT EXISTS idx_know_knower ON knowledge_edges(knower_entity_id, secret_key, learned_chapter);
CREATE INDEX IF NOT EXISTS idx_know_secret ON knowledge_edges(secret_key);
CREATE INDEX IF NOT EXISTS idx_know_public ON knowledge_edges(secret_key, public_from_chapter);
CREATE INDEX IF NOT EXISTS idx_know_srcfact ON knowledge_edges(source_fact_id);

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
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(item_entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id)
);
CREATE INDEX IF NOT EXISTS idx_itemlog_item ON item_log(item_entity_id, change_chapter);
CREATE INDEX IF NOT EXISTS idx_itemlog_srcfact ON item_log(source_fact_id);

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
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    FOREIGN KEY(owner_entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id),
    CHECK(cost_json IS NULL OR json_valid(cost_json)),
    CHECK(constraint_json IS NULL OR json_valid(constraint_json))
);
CREATE INDEX IF NOT EXISTS idx_gimrule_srcfact ON gimmick_rules(source_fact_id);

CREATE TABLE IF NOT EXISTS gimmick_usage_log (
    id              TEXT PRIMARY KEY,
    gimmick_id      TEXT NOT NULL,
    user_entity_id  TEXT NOT NULL,
    use_chapter     INTEGER NOT NULL,
    use_story_time  INTEGER,
    outcome         TEXT,
    paid_cost_json  TEXT,
    fact_id         TEXT,
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(gimmick_id)     REFERENCES gimmick_rules(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id),
    FOREIGN KEY(user_entity_id) REFERENCES entities(id),
    CHECK(paid_cost_json IS NULL OR json_valid(paid_cost_json))
);
CREATE INDEX IF NOT EXISTS idx_gimuse_gimmick ON gimmick_usage_log(gimmick_id, use_chapter);
CREATE INDEX IF NOT EXISTS idx_gimuse_srcfact ON gimmick_usage_log(source_fact_id);

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
    source_fact_id  TEXT,                          -- §16/A4: 投影来源 canon fact（retcon 级联 / 重投影）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_fact_id) REFERENCES facts(id)
);
CREATE INDEX IF NOT EXISTS idx_numf_entity ON numeric_facts(entity_id, metric_key, as_of_chapter);
CREATE INDEX IF NOT EXISTS idx_numf_srcfact ON numeric_facts(source_fact_id);

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
    value_axis      TEXT,                          -- §10 R12/B6: 价值轴标签(如"认可 vs 轻视")
    arc_id          TEXT,                          -- §10 R12/B2: 所属情节弧线 ID
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

-- pacing_cursor: 单行累积态(PacingController 游标); pacing_state 保留逐章快照(§10 R12/B4)
CREATE TABLE IF NOT EXISTS pacing_cursor (
    id                        INTEGER PRIMARY KEY CHECK(id = 1),   -- 单作品单行
    chapters_since_big_payoff INTEGER NOT NULL DEFAULT 0,
    kchars_since_small_payoff REAL    NOT NULL DEFAULT 0,
    buildup                   INTEGER NOT NULL DEFAULT 0,           -- 蓄力值
    recent_high_streak        INTEGER NOT NULL DEFAULT 0,
    updated_at                TEXT    NOT NULL DEFAULT (datetime('now'))
);

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
    volume_no       INTEGER,                        -- §9.4 归属卷号
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
    cold_start      INTEGER NOT NULL DEFAULT 0,     -- §9.4 1=由冷启动反向抽取生成
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
    candidate_id    TEXT,
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

-- 9b) 工具调用审计(§12.5,append-only 证据链) --------------------------------
CREATE TABLE IF NOT EXISTS tool_call_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL,        -- = skill_run_log.run_id
    chapter        INTEGER NOT NULL,
    skill          TEXT    NOT NULL,        -- skill_name@version
    step           INTEGER NOT NULL,        -- ReAct 第几步(1..max_tool_steps)
    tool_name      TEXT    NOT NULL,
    args_json      TEXT    NOT NULL,        -- 归一化入参(json_valid CHECK)
    result_digest  TEXT    NOT NULL,        -- content sha256 前 16
    latency_ms     INTEGER NOT NULL,
    provider       TEXT,                    -- 本步 LLM 供应商
    model          TEXT,                    -- 本步模型 ID
    note           TEXT,                    -- fresh / cache_hit / empty / degraded
    ts             TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (json_valid(args_json))
);
CREATE INDEX IF NOT EXISTS idx_tcl_run    ON tool_call_log(run_id, step);
CREATE INDEX IF NOT EXISTS idx_tcl_chap   ON tool_call_log(chapter, tool_name);

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

-- 12) volumes + branches (§9.4 MVP3) -----------------------------------------
-- 卷：按章节范围划分的叙事单元，World State 在卷间通过 as-of 投影自然续接。
CREATE TABLE IF NOT EXISTS volumes (
    id              TEXT PRIMARY KEY,
    volume_no       INTEGER NOT NULL,           -- 卷序号，从 1 起
    title           TEXT NOT NULL,
    synopsis        TEXT,
    start_chapter   INTEGER,                    -- 第一章（含）
    end_chapter     INTEGER,                    -- 最后一章（含），NULL = 仍在写
    status          TEXT NOT NULL DEFAULT 'writing'
                        CHECK(status IN ('writing','completed','archived')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(volume_no)
);
CREATE INDEX IF NOT EXISTS idx_volumes_no ON volumes(volume_no);

-- 分支：从某章分叉的平行线（支线/IF 结局）
CREATE TABLE IF NOT EXISTS branches (
    id              TEXT PRIMARY KEY,
    branch_name     TEXT NOT NULL UNIQUE,
    fork_chapter    INTEGER NOT NULL,           -- 从哪一章分叉
    base_branch_id  TEXT,                       -- NULL = 主线；否则指向父分支
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','merged','abandoned')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(base_branch_id) REFERENCES branches(id)
);
CREATE INDEX IF NOT EXISTS idx_branches_fork ON branches(fork_chapter);

-- 13) pipeline_run 状态机表（F6 崩溃幂等恢复）-------------------------------
-- 每次 generate_chapter() 开始时插入 'running'，完成后更新为 'completed'。
-- 启动期 sweep_crashed_runs() 将残留 'running' 行标为 'crashed'。
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id          TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    project_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('running','completed','crashed')),
    draft_id        TEXT,   -- draft_index.id，成功后填入
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_chapter ON pipeline_run(project_id, chapter);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_status  ON pipeline_run(status);

-- 14) sessions + turns + turn_events（§13.2 会话/turn 模型 + SSE 断线续传）--------
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    client        TEXT NOT NULL
                     CHECK(client IN ('cli','web','chat','api')),
    mode          TEXT
                     CHECK(mode IN ('human_gate','auto_promote','hybrid')),
    actor         TEXT NOT NULL,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,
    budget_spent_tokens INTEGER NOT NULL DEFAULT 0,
    budget_spent_usd    REAL    NOT NULL DEFAULT 0.0,
    summary       TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL
                     CHECK(kind IN ('command','chat','long_task')),
    intent        TEXT,
    request_json  TEXT NOT NULL,
    routed_endpoint TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                     CHECK(status IN ('running','done','error','canceled')),
    stream        INTEGER NOT NULL DEFAULT 0,
    result_json   TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);

CREATE TABLE IF NOT EXISTS turn_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id       TEXT NOT NULL REFERENCES turns(id),
    event_type    TEXT NOT NULL,  -- phase|progress|draft_token|check_issue|gate_decision|result|error
    data_json     TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_turn_events_turn ON turn_events(turn_id, id);

-- 新增列由迁移器在已有库上追加（见 db/migrations.py）；
-- 新建库通过 CREATE TABLE 定义直接包含（见 facts/draft_index/l1_atoms 表定义）。
-- 索引（可在已有库上安全重建）：
CREATE INDEX IF NOT EXISTS idx_facts_volume ON facts(volume_no);
CREATE INDEX IF NOT EXISTS idx_facts_branch ON facts(branch_id);
CREATE INDEX IF NOT EXISTS idx_draft_volume ON draft_index(volume_no);
