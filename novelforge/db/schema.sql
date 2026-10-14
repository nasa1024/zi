-- NovelForge novel.db schema (MVP0 subset of design §02).
-- Single source of truth = SQLite. markdown is read-only render output.
-- scene_vec uses a pure-SQLite fallback table in this baseline; sqlite-vec/vec0 can
-- replace its internals later without changing the caller contract.
-- Build order follows §2.10 (referenced tables first). FK enforcement is per-row at DML time,
-- so forward references in CREATE are fine; PRAGMAs are set by connection.py (not here).

-- 1) global metadata / version binding ----------------------------------------
CREATE TABLE IF NOT EXISTS meta_kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v21: project-scoped, append-only writing profile history.  meta_kv keeps the
-- current projection; this table preserves the exact profile used over time.
CREATE TABLE IF NOT EXISTS writing_profile_versions (
    id                     TEXT PRIMARY KEY,
    revision               INTEGER NOT NULL UNIQUE CHECK(revision >= 1),
    profile_id             TEXT NOT NULL,
    catalog_version        TEXT NOT NULL,
    content_language       TEXT NOT NULL,
    target_market          TEXT NOT NULL,
    publication_format     TEXT NOT NULL,
    primary_genre          TEXT,
    story_tags_json        TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(story_tags_json)),
    audience_rating        TEXT NOT NULL DEFAULT 'general',
    content_warnings_json  TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(content_warnings_json)),
    overrides_json         TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(overrides_json)),
    snapshot_json          TEXT NOT NULL CHECK(json_valid(snapshot_json)),
    effective_from_chapter INTEGER NOT NULL CHECK(effective_from_chapter >= 1),
    prompt_version         TEXT NOT NULL,
    changed_by             TEXT NOT NULL DEFAULT 'user',
    change_reason          TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_writing_profile_versions_effective
    ON writing_profile_versions(effective_from_chapter, revision DESC);
CREATE TRIGGER IF NOT EXISTS trg_writing_profile_versions_no_update
    BEFORE UPDATE ON writing_profile_versions
    BEGIN SELECT RAISE(ABORT, 'writing_profile_versions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_writing_profile_versions_no_delete
    BEFORE DELETE ON writing_profile_versions
    BEGIN SELECT RAISE(ABORT, 'writing_profile_versions is append-only'); END;

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
    last_mentioned_chapter INTEGER,                -- v12: 最近被提及章（mention 不改 state）
    advance_count   INTEGER NOT NULL DEFAULT 0,    -- v12: 实质推进次数
    last_advanced_chapter INTEGER,                 -- v12: 最近实质推进章
    origin          TEXT NOT NULL DEFAULT 'manual',-- v12: manual=人工建档 settle=结算仲裁自动建档
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- v22/F1 读者承诺账本：承诺类型（MICE/线索/誓约…）、长短线、读者可见性、嵌套父承诺、提醒周期
    promise_kind    TEXT,
    line_type       TEXT NOT NULL DEFAULT 'long'
                        CHECK(line_type IN ('long','short','micro')),
    visibility      TEXT NOT NULL DEFAULT 'reader'
                        CHECK(visibility IN ('reader','hidden')),
    parent_id       TEXT REFERENCES foreshadow(id),
    remind_every    INTEGER CHECK(remind_every IS NULL OR remind_every >= 1),
    FOREIGN KEY(related_entity_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS idx_fs_state ON foreshadow(state);
CREATE INDEX IF NOT EXISTS idx_fs_due   ON foreshadow(due_chapter);

-- v12: 伏笔结算审计轨迹（P1#6——mention/advance 二分防假回收）
CREATE TABLE IF NOT EXISTS foreshadow_log (
    id            TEXT PRIMARY KEY,
    foreshadow_id TEXT NOT NULL REFERENCES foreshadow(id),
    chapter       INTEGER NOT NULL,
    action        TEXT NOT NULL CHECK(action IN ('plant','mention','advance','payoff')),
    evidence      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fslog_fs ON foreshadow_log(foreshadow_id, chapter);

CREATE TABLE IF NOT EXISTS beats (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    beat_type       TEXT NOT NULL
                        CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point',
                                            'aftermath','hook')),   -- v22: +aftermath（余韵）
    summary         TEXT NOT NULL,
    value_start     TEXT,
    value_end       TEXT,
    value_axis      TEXT,                          -- §10 R12/B6: 价值轴标签(如"认可 vs 轻视")
    arc_id          TEXT,                          -- §10 R12/B2: 所属情节弧线 ID
    source_decision_id TEXT,                       -- v16: 产出本 beat 的选择节点
    constraint_level TEXT NOT NULL DEFAULT 'soft'  -- v16: future plan 约束强度
                        CHECK(constraint_level IN ('soft','hard')),
    tension_level   INTEGER,
    related_foreshadow_id TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','drafted','satisfied','dropped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    witnesses_json  TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(witnesses_json)), -- v22/F5
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
    target_emotion    TEXT,                        -- v13: 本章目标情绪词
    opening_hook_type TEXT,                        -- v13: 章首钩子（7 式或 other）
    hook_type         TEXT,                        -- v13: 章尾钩子（13 式或 other）
    expectation_score INTEGER,                     -- v13: 期待度 1-5
    plan_version    INTEGER NOT NULL DEFAULT 1,     -- v16: 滚动规划版本
    locked          INTEGER NOT NULL DEFAULT 0,     -- v16: 1=用户/Agent 已确认，不得静默重写
    source_decision_id TEXT,                        -- v16: 对应 decision_sets.id
    arc_targets_json TEXT NOT NULL DEFAULT '[]',    -- v20: 本章服务的长期情节弧
    volume_kr_targets_json TEXT NOT NULL DEFAULT '[]', -- v20: 本章服务的卷 KR
    milestone_targets_json TEXT NOT NULL DEFAULT '[]', -- v20: 本章服务的里程碑
    must_advance_json TEXT NOT NULL DEFAULT '[]',   -- v20: 必须推进
    must_resolve_json TEXT NOT NULL DEFAULT '[]',   -- v20: 必须解决
    must_include_entities_json TEXT NOT NULL DEFAULT '[]', -- v20: 必须出场实体
    must_not_reveal_json TEXT NOT NULL DEFAULT '[]', -- v20: 禁止提前揭示
    must_not_resolve_json TEXT NOT NULL DEFAULT '[]', -- v20: 禁止本章解决
    forbidden_state_changes_json TEXT NOT NULL DEFAULT '[]', -- v20: 禁止状态变化
    target_state_json TEXT NOT NULL DEFAULT '{}',   -- v20: 章节完成后的目标状态
    success_criteria_json TEXT NOT NULL DEFAULT '[]', -- v20: 可验证完成标准
    current_plan_version_id TEXT,                   -- v20: 当前权威计划版本
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(pov_entity_id) REFERENCES entities(id),
    FOREIGN KEY(current_plan_version_id) REFERENCES chapter_plan_versions(id),
    CHECK(json_valid(arc_targets_json)),
    CHECK(json_valid(volume_kr_targets_json)),
    CHECK(json_valid(milestone_targets_json)),
    CHECK(json_valid(must_advance_json)),
    CHECK(json_valid(must_resolve_json)),
    CHECK(json_valid(must_include_entities_json)),
    CHECK(json_valid(must_not_reveal_json)),
    CHECK(json_valid(must_not_resolve_json)),
    CHECK(json_valid(forbidden_state_changes_json)),
    CHECK(json_valid(target_state_json)),
    CHECK(json_valid(success_criteria_json))
);

-- v13: 文风锚点（P1#9——手动录入范文段，按情绪做 few-shot）
CREATE TABLE IF NOT EXISTS style_anchors (
    id               TEXT PRIMARY KEY,
    emotion          TEXT NOT NULL,
    title            TEXT,
    content          TEXT NOT NULL,
    language         TEXT NOT NULL DEFAULT 'zh-CN',
    source_type      TEXT NOT NULL DEFAULT 'legacy',
    source_note      TEXT,
    rights_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(rights_confirmed IN (0,1)),
    profile_tags     TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(profile_tags)),
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_style_anchors_emotion ON style_anchors(emotion);
CREATE INDEX IF NOT EXISTS idx_style_anchors_language_emotion
    ON style_anchors(language, emotion);

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
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- v22/F3: 本章实际转向（+ / - / ±）与转折类型（action / revelation），逐章幂等 upsert
    polarity        TEXT CHECK(polarity IS NULL OR polarity IN ('+','-','±')),
    turning_point   TEXT CHECK(turning_point IS NULL OR turning_point IN ('action','revelation'))
);
CREATE INDEX IF NOT EXISTS idx_pacing_chapter ON pacing_state(chapter);

-- pacing_cursor: 单行累积态(PacingController 游标); pacing_state 保留逐章快照(§10 R12/B4)
CREATE TABLE IF NOT EXISTS pacing_cursor (
    id                        INTEGER PRIMARY KEY CHECK(id = 1),   -- 单作品单行
    chapters_since_big_payoff INTEGER NOT NULL DEFAULT 0,
    kchars_since_small_payoff REAL    NOT NULL DEFAULT 0,
    buildup                   INTEGER NOT NULL DEFAULT 0,           -- 蓄力值
    recent_high_streak        INTEGER NOT NULL DEFAULT 0,
    updated_at                TEXT    NOT NULL DEFAULT (datetime('now')),
    low_streak                INTEGER NOT NULL DEFAULT 0,           -- v22/F3: 连续低谷章数
    polarity_streak           INTEGER NOT NULL DEFAULT 0,           -- v22/F3: 同向转向连续章数
    last_polarity             TEXT                                  -- v22/F3: 上一章实际转向
);

-- 7b) 分层叙事摘要（M2-②）：章摘要 → 卷滚动摘要(volumes.rolling_summary) → 全局梗概(meta_kv)
-- 每章 COMMIT 后由 FAST tier 生成 150-300 字摘要；召回时按 chapter<=as_of 注入
-- 最近 N 章（防剧透语义与其余查询一致）。
CREATE TABLE IF NOT EXISTS chapter_summaries (
    id          TEXT PRIMARY KEY,
    chapter     INTEGER NOT NULL UNIQUE,
    summary     TEXT NOT NULL,
    volume_no   INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chapter_summaries_chapter ON chapter_summaries(chapter);

-- 8) L0 draft index + L1/L2 source tables ------------------------------------
CREATE TABLE IF NOT EXISTS draft_index (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    revision_round  INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    word_count      INTEGER,
    content_language TEXT,
    writing_profile_id TEXT,
    writing_profile_version INTEGER,
    prompt_version  TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','checked','revised','committed','archived')),
    volume_no       INTEGER,                        -- §9.4 归属卷号
    fts_indexed_at  TEXT,                           -- L0 派生 FTS 最近成功时间
    l1_status       TEXT NOT NULL DEFAULT 'pending'
                        CHECK(l1_status IN ('pending','completed','failed')),
    l1_extracted_at TEXT,
    l2_status       TEXT NOT NULL DEFAULT 'pending'
                        CHECK(l2_status IN ('pending','completed','failed')),
    l2_aggregated_at TEXT,
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
    atom_type       TEXT,
    payload_json    TEXT,
    source_line_start INTEGER,
    source_line_end   INTEGER,
    content_hash    TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','unanchored','superseded')),
    cold_start      INTEGER NOT NULL DEFAULT 0,     -- §9.4 1=由冷启动反向抽取生成
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(draft_id) REFERENCES draft_index(id)
);
CREATE INDEX IF NOT EXISTS idx_l1_chapter ON l1_atoms(chapter);
CREATE INDEX IF NOT EXISTS idx_l1_candidate ON l1_atoms(candidate_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_l1_draft_hash
    ON l1_atoms(draft_id, content_hash)
    WHERE draft_id IS NOT NULL AND content_hash IS NOT NULL;

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

-- scene_vec：L2 场景摘要的向量召回回退存储（v15，§06.4.3）。
-- sqlite-vec(vec0) 为可选快路；未装时用本表 + 暴力余弦。embedding 以 JSON 数组存。
CREATE TABLE IF NOT EXISTS scene_vec (
    scene_id    TEXT PRIMARY KEY,           -- = l2_scenes.id
    chapter     INTEGER NOT NULL,
    dim         INTEGER NOT NULL,
    vec_json    TEXT NOT NULL,
    model       TEXT,
    version     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scene_vec_chapter ON scene_vec(chapter);

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
    source_draft_id TEXT,
    source_skill    TEXT,
    decided_by      TEXT,
    decided_at      TEXT,
    committed_revision_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(target_fact_id) REFERENCES facts(id),
    FOREIGN KEY(entity_id) REFERENCES entities(id),
    FOREIGN KEY(source_draft_id) REFERENCES draft_index(id),
    CHECK(proposal_json IS NULL OR json_valid(proposal_json)),
    CHECK(evidence_refs IS NULL OR json_valid(evidence_refs)),
    CHECK(conflict_flags IS NULL OR json_valid(conflict_flags))
);
CREATE INDEX IF NOT EXISTS idx_cand_status  ON fact_candidates(status);
CREATE INDEX IF NOT EXISTS idx_cand_risk    ON fact_candidates(risk_tier);
CREATE INDEX IF NOT EXISTS idx_cand_chapter ON fact_candidates(source_chapter);
CREATE INDEX IF NOT EXISTS idx_cand_source_draft ON fact_candidates(source_draft_id);
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
    rolling_summary TEXT,                       -- M2-②: 本卷至今滚动摘要（FAST tier 每5章更新）
    objective       TEXT,                        -- v14: 本卷可验证目标（P2#13）
    key_results     TEXT,                        -- v14: JSON [{id,text,status,evidence}]
    source_decision_id TEXT,                     -- v17: Agent durable replay 幂等键
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(volume_no)
);
CREATE INDEX IF NOT EXISTS idx_volumes_no ON volumes(volume_no);
CREATE INDEX IF NOT EXISTS idx_volumes_source_decision
    ON volumes(source_decision_id);

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
    detail_json     TEXT,   -- M3-①: 候选择优报告等运行明细（JSON）
    quality_score   REAL,   -- M5-⑦: 章节质量分 0-10（LLM 评委）
    tokens_spent    INTEGER, -- v11: 逐章成本（账本净增量）
    usd_spent       REAL,    -- v11: 逐章成本（估算 USD）
    content_language TEXT,   -- v21: immutable generation context
    writing_profile_id TEXT,
    writing_profile_version INTEGER,
    prompt_version  TEXT,
    model_id        TEXT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    batch_id        TEXT    -- v22/F6: 多章连写批次（draft_batches.id）；单章流程为 NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_chapter ON pipeline_run(project_id, chapter);
CREATE INDEX IF NOT EXISTS idx_pipeline_run_status  ON pipeline_run(status);

-- 13a) v22/F6 多章连写：一次长输出调用起草连续多章，逐章校验后才进入 draft_index。
-- 未处理/被丢弃的章节正文只留在 draft_batch_chapters，不推进 next_chapter_no。
CREATE TABLE IF NOT EXISTS draft_batches (
    id              TEXT PRIMARY KEY,
    start_chapter   INTEGER NOT NULL CHECK(start_chapter >= 1),
    end_chapter     INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                        CHECK(status IN ('running','completed','partial','failed','canceled')),
    origin          TEXT NOT NULL DEFAULT 'api',     -- api | autopilot | agent
    model_id        TEXT,
    tokens_spent    INTEGER NOT NULL DEFAULT 0,
    usd_spent       REAL    NOT NULL DEFAULT 0,
    detail_json     TEXT    NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK(end_chapter >= start_chapter)
);
CREATE INDEX IF NOT EXISTS idx_draft_batches_status ON draft_batches(status);

CREATE TABLE IF NOT EXISTS draft_batch_chapters (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES draft_batches(id),
    chapter         INTEGER NOT NULL,
    seq             INTEGER NOT NULL CHECK(seq >= 1),
    draft_text      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','accepted','rejected','discarded')),
    run_id          TEXT,                               -- 逐章校验对应的 pipeline_run
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(batch_id, chapter)
);
CREATE INDEX IF NOT EXISTS idx_draft_batch_chapters_chapter
    ON draft_batch_chapters(chapter, status);

-- 13b) autopilot_sessions 持久化（M1-③ 断点恢复）------------------------------
-- AutopilotManager 写穿：start 时 INSERT，每章完成 UPDATE，终态 UPDATE。
-- 进程重启后 running/degraded 残留行由 list 路径标记为 'interrupted'，
-- 用户经 /autopilot/{sid}/resume 显式恢复（新会话，resumed_from 指向旧会话）。
CREATE TABLE IF NOT EXISTS autopilot_sessions (
    session_id      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    from_chapter    INTEGER NOT NULL,
    to_chapter      INTEGER NOT NULL,
    current_chapter INTEGER NOT NULL,
    status          TEXT NOT NULL,
    policy_mode     TEXT NOT NULL,
    chapters_done   INTEGER NOT NULL DEFAULT 0,
    budget_tokens_total INTEGER NOT NULL DEFAULT 0,
    budget_usd_total    REAL    NOT NULL DEFAULT 0.0,
    consecutive_hard_issues INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    req_json        TEXT NOT NULL DEFAULT '{}',
    resumed_from    TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    heartbeat_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_autopilot_sessions_status
    ON autopilot_sessions(project_id, status);

-- 14) sessions + turns + turn_events（§13.2 会话/turn 模型 + SSE 断线续传）--------
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    client        TEXT NOT NULL
                     CHECK(client IN ('cli','web','chat','api')),
    mode          TEXT
                     CHECK(mode IN ('human_gate','auto_promote','hybrid')),
    interaction_mode TEXT NOT NULL DEFAULT 'guided'
                     CHECK(interaction_mode IN ('guided','copilot','autonomous')),
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

-- 15) 选择驱动 Agent + 分层剧情计划（v16）--------------------------------------
-- 剧情计划是“作者意图”，不是 canon；未来内容只能经 PlanContext 作为创作护栏读取。
CREATE TABLE IF NOT EXISTS story_plans (
    id              TEXT PRIMARY KEY,
    version         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    premise         TEXT,
    ending_direction TEXT,
    phase_summary   TEXT,
    constraints_json TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded','archived')),
    source_decision_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(version),
    CHECK(json_valid(constraints_json))
);
CREATE INDEX IF NOT EXISTS idx_story_plans_status ON story_plans(status, version);
CREATE INDEX IF NOT EXISTS idx_story_plans_source_decision
    ON story_plans(source_decision_id);

CREATE TABLE IF NOT EXISTS story_arcs (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES story_plans(id),
    arc_type        TEXT NOT NULL DEFAULT 'main'
                        CHECK(arc_type IN ('main','character','mystery','romance','foreshadow','world','other')),
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    objective       TEXT,
    start_chapter   INTEGER NOT NULL,
    end_chapter     INTEGER,
    priority        INTEGER NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','active','completed','deferred','abandoned')),
    constraints_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(end_chapter IS NULL OR end_chapter >= start_chapter),
    CHECK(json_valid(constraints_json))
);
CREATE INDEX IF NOT EXISTS idx_story_arcs_window ON story_arcs(start_chapter, end_chapter, status);
CREATE INDEX IF NOT EXISTS idx_story_arcs_plan ON story_arcs(plan_id, priority);

CREATE TABLE IF NOT EXISTS arc_milestones (
    id              TEXT PRIMARY KEY,
    arc_id          TEXT NOT NULL REFERENCES story_arcs(id),
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    due_start_chapter INTEGER,
    due_end_chapter INTEGER,
    not_before_chapter INTEGER,
    target_state_json TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','active','completed','deferred','invalidated')),
    completed_chapter INTEGER,
    source_decision_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(due_end_chapter IS NULL OR due_start_chapter IS NULL OR due_end_chapter >= due_start_chapter),
    CHECK(json_valid(target_state_json))
);
CREATE INDEX IF NOT EXISTS idx_arc_milestones_due
    ON arc_milestones(status, due_start_chapter, due_end_chapter, not_before_chapter);
CREATE INDEX IF NOT EXISTS idx_arc_milestones_arc ON arc_milestones(arc_id, status);

CREATE TABLE IF NOT EXISTS decision_sets (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    stage           TEXT NOT NULL,
    scope_type      TEXT NOT NULL DEFAULT 'project'
                        CHECK(scope_type IN ('project','story','volume','chapter','draft','replan')),
    scope_id        TEXT,
    question        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'awaiting_choice'
                        CHECK(status IN ('awaiting_choice','selected','auto_selected','superseded','canceled')),
    recommended_option_id TEXT,
    selected_option_id TEXT,
    context_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    CHECK(json_valid(context_json))
);
CREATE INDEX IF NOT EXISTS idx_decision_sets_session ON decision_sets(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_sets_status ON decision_sets(status, stage);
CREATE INDEX IF NOT EXISTS idx_decision_sets_pending_scope
    ON decision_sets(session_id, stage, scope_type, scope_id, status);

CREATE TABLE IF NOT EXISTS decision_options (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decision_sets(id),
    option_index    INTEGER NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    rationale       TEXT,
    impact_json     TEXT NOT NULL DEFAULT '{}',
    score           REAL,
    selected        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(decision_id, option_index),
    CHECK(json_valid(payload_json)),
    CHECK(json_valid(impact_json))
);
CREATE INDEX IF NOT EXISTS idx_decision_options_decision ON decision_options(decision_id, option_index);

CREATE TABLE IF NOT EXISTS plan_revisions (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT REFERENCES story_plans(id),
    source_chapter  INTEGER,
    reason          TEXT NOT NULL,
    divergence_score REAL NOT NULL DEFAULT 0.0,
    before_json     TEXT NOT NULL DEFAULT '{}',
    after_json      TEXT NOT NULL DEFAULT '{}',
    source_decision_id TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(json_valid(before_json)),
    CHECK(json_valid(after_json))
);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id, created_at);

-- 16) 大纲契约版本、章节实际结果与偏航对账（v20）---------------------------
-- chapter_cards 只保存当前投影；本表是 append-only 的章节计划审计源。
CREATE TABLE IF NOT EXISTS chapter_plan_versions (
    id                         TEXT PRIMARY KEY,
    chapter                    INTEGER NOT NULL,
    version                    INTEGER NOT NULL CHECK(version >= 1),
    payload_json               TEXT NOT NULL DEFAULT '{}',
    status                     TEXT NOT NULL DEFAULT 'candidate'
                                   CHECK(status IN ('candidate','active','superseded',
                                                    'executed','abandoned')),
    source_decision_id         TEXT,
    source_alignment_report_id TEXT,
    based_on_chapter           INTEGER,
    reason                     TEXT,
    divergence_score           REAL
                                   CHECK(divergence_score IS NULL OR
                                         (divergence_score >= 0.0 AND divergence_score <= 1.0)),
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at               TEXT,
    superseded_at              TEXT,
    UNIQUE(chapter, version),
    CHECK(json_valid(payload_json))
);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_chapter
    ON chapter_plan_versions(chapter, version DESC);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_status
    ON chapter_plan_versions(chapter, status);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_source_decision
    ON chapter_plan_versions(source_decision_id);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_versions_source_alignment
    ON chapter_plan_versions(source_alignment_report_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapter_plan_versions_one_active
    ON chapter_plan_versions(chapter) WHERE status='active';

-- 版本化 beat 快照不复用 beats(chapter,seq) 的唯一键，旧计划因此无需删除。
CREATE TABLE IF NOT EXISTS chapter_plan_version_beats (
    id               TEXT PRIMARY KEY,
    plan_version_id  TEXT NOT NULL REFERENCES chapter_plan_versions(id),
    seq              INTEGER NOT NULL CHECK(seq >= 1),
    beat_type        TEXT NOT NULL
                         CHECK(beat_type IN ('setup','turn','payoff_beat',
                                             'tension_point','aftermath','hook')),
    summary          TEXT NOT NULL,
    value_axis       TEXT,
    arc_id           TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft'
                         CHECK(constraint_level IN ('soft','hard')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    witnesses_json   TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(witnesses_json)), -- v22/F5
    UNIQUE(plan_version_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_version
    ON chapter_plan_version_beats(plan_version_id, seq);
CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_arc
    ON chapter_plan_version_beats(arc_id);

-- 同章可有多次抽取，但仅一条 status=current 是权威实际结果。
CREATE TABLE IF NOT EXISTS chapter_actuals (
    id                TEXT PRIMARY KEY,
    chapter           INTEGER NOT NULL,
    draft_id          TEXT NOT NULL REFERENCES draft_index(id),
    payload_json      TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'current'
                          CHECK(status IN ('current','superseded')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at     TEXT,
    UNIQUE(draft_id, extractor_version),
    CHECK(json_valid(payload_json))
);
CREATE INDEX IF NOT EXISTS idx_chapter_actuals_chapter
    ON chapter_actuals(chapter, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chapter_actuals_draft
    ON chapter_actuals(draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapter_actuals_one_current
    ON chapter_actuals(chapter) WHERE status='current';

-- 对账报告同样保留历史，只允许每章一条 current 权威报告。
CREATE TABLE IF NOT EXISTS plan_alignment_reports (
    id                TEXT PRIMARY KEY,
    chapter           INTEGER NOT NULL,
    plan_version_id   TEXT REFERENCES chapter_plan_versions(id),
    actuals_id         TEXT NOT NULL REFERENCES chapter_actuals(id),
    goal_score         REAL NOT NULL CHECK(goal_score >= 0.0 AND goal_score <= 1.0),
    beat_score         REAL NOT NULL CHECK(beat_score >= 0.0 AND beat_score <= 1.0),
    arc_score          REAL NOT NULL CHECK(arc_score >= 0.0 AND arc_score <= 1.0),
    volume_score       REAL NOT NULL CHECK(volume_score >= 0.0 AND volume_score <= 1.0),
    reveal_score       REAL NOT NULL CHECK(reveal_score >= 0.0 AND reveal_score <= 1.0),
    divergence_score   REAL NOT NULL
                           CHECK(divergence_score >= 0.0 AND divergence_score <= 1.0),
    divergence_level   TEXT NOT NULL
                           CHECK(divergence_level IN ('normal','minor','major','severe')),
    confidence         REAL CHECK(confidence IS NULL OR
                                   (confidence >= 0.0 AND confidence <= 1.0)),
    decision           TEXT CHECK(decision IS NULL OR
                                  decision IN ('keep','patch','replan','pause')),
    detail_json        TEXT NOT NULL DEFAULT '{}',
    evaluator_version  TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'current'
                           CHECK(status IN ('current','superseded')),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_at      TEXT,
    UNIQUE(plan_version_id, actuals_id, evaluator_version),
    CHECK(json_valid(detail_json))
);
CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_chapter
    ON plan_alignment_reports(chapter, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_plan_version
    ON plan_alignment_reports(plan_version_id);
CREATE INDEX IF NOT EXISTS idx_plan_alignment_reports_actuals
    ON plan_alignment_reports(actuals_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_alignment_reports_one_current
    ON plan_alignment_reports(chapter) WHERE status='current';

-- 新增列由迁移器在已有库上追加（见 db/migrations.py）；
-- 新建库通过 CREATE TABLE 定义直接包含（见 facts/draft_index/l1_atoms 表定义）。
-- 索引（可在已有库上安全重建）：
CREATE INDEX IF NOT EXISTS idx_facts_volume ON facts(volume_no);
CREATE INDEX IF NOT EXISTS idx_facts_branch ON facts(branch_id);
CREATE INDEX IF NOT EXISTS idx_draft_volume ON draft_index(volume_no);
