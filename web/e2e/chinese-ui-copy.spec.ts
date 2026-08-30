import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const project = {
  project_id: 'locale-book',
  name: '雾港回声',
  genre: 'xuanhuan',
  primary_genre: 'xuanhuan',
  story_tags: ['progression_fantasy'],
  premise: '一个守夜人发现，每场浓雾都会重写一段城市记忆。',
  db_path: '/tmp/locale-book.db',
  created_at: '2026-08-30T08:00:00Z',
  chapter_count: 4,
  canon_fact_count: 12,
  archived: false,
  content_language: 'zh-CN',
  target_market: 'cn_web',
  publication_format: 'web_serial',
  audience_rating: 'general',
  writing_profile_id: 'cn_web_novel_v1',
  writing_profile_name: '中文网文',
  writing_profile_version: 3,
  writing_profile_effective_from_chapter: 1,
};

const volume = {
  id: 'volume-1',
  volume_no: 1,
  title: '雾中旧港',
  synopsis: '主角追查被重写的港口记忆。',
  start_chapter: 1,
  end_chapter: 12,
  status: 'writing',
  created_at: '2026-08-30T08:00:00Z',
  objective: '确认浓雾改写记忆的规律',
  key_results: [{ id: 'kr-1', text: '找到第一名清醒者', status: 'met', evidence: '第四章已出现' }],
};

const storyMap = {
  plan: {
    id: 'plan-1',
    version: 2,
    title: '追查雾港真相',
    premise: project.premise,
    ending_direction: '守夜人保住城市的真实记忆。',
    phase_summary: '从异常广播追到旧港档案。',
    status: 'active',
  },
  arcs: [{
    id: 'arc-1',
    arc_type: 'character',
    title: '守夜人的选择',
    summary: '他必须在安稳的假记忆和危险的真相之间选择。',
    objective: '承担守护真实的代价',
    start_chapter: 1,
    end_chapter: 12,
    priority: 1,
    status: 'active',
    milestones: [{
      id: 'milestone-1',
      arc_id: 'arc-1',
      title: '第一次拒绝遗忘',
      summary: '主角留下不会被浓雾抹去的证据。',
      due_start_chapter: 4,
      due_end_chapter: 6,
      status: 'pending',
      target_state: { knowledge_edges: ['主角知道档案室位置'] },
    }],
  }],
  volumes: [volume],
  chapters: [
    { chapter: 4, title: '不熄的灯', goal: '留下证据', status: 'drafted', plan_version: 2, locked: true },
    { chapter: 5, title: '旧港档案', goal: '进入封存区', status: 'planned', plan_version: 2, locked: true },
  ],
};

const finishedSession = {
  session_id: 'session-locale-finished',
  project_id: project.project_id,
  interaction_mode: 'guided',
  stage: 'finished',
  status: 'finished',
  decision: null,
  last_decision: null,
  state: { current_chapter: 5 },
  story_map: storyMap,
  error: null,
};

const activeAgentSession = {
  session_id: 'session-locale-active',
  project_id: project.project_id,
  interaction_mode: 'guided',
  stage: 'chapter_action',
  status: 'awaiting_choice',
  decision: {
    decision_id: 'decision-locale-active',
    stage: 'chapter_action',
    scope_type: 'chapter',
    scope_id: '5',
    question: 'Chapter 5 is planned. What should happen next?',
    status: 'open',
    recommended_option_id: 'option-generate',
    selected_option_id: null,
    context: {},
    options: [
      {
        id: 'option-generate',
        decision_id: 'decision-locale-active',
        option_index: 0,
        title: 'Generate Chapter 5',
        summary: 'Generate three drafts from the selected chapter contract and preselect the strongest one.',
        payload: { action: 'generate_chapter', chapter: 5 },
        rationale: 'The current route is ready for drafting.',
        impact: {
          candidate_count: 3,
          uses_future_guardrails: true,
          is_current_winner: true,
        },
        score: 9.2,
        selected: false,
      },
      {
        id: 'option-replan',
        decision_id: 'decision-locale-active',
        option_index: 1,
        title: 'Replan the future route',
        summary: 'Keep established facts and regenerate the future chapter window.',
        payload: { action: 'replan' },
        rationale: 'Use this when the current route no longer fits the story.',
        impact: {
          canon_changed: false,
          future_plan_changed: true,
          source: 'volume_api',
          key_results: [{
            id: 'kr-2',
            text: '保护港口记录',
            status: 'pending',
          }],
        },
        score: 7.5,
        selected: false,
      },
      {
        id: 'option-finish',
        decision_id: 'decision-locale-active',
        option_index: 2,
        title: 'Finish for now',
        summary: 'Save the session and all plans so work can resume from this point.',
        payload: { action: 'finish' },
        rationale: null,
        impact: { reversible: true },
        score: 5,
        selected: false,
      },
    ],
  },
  last_decision: null,
  state: { current_chapter: 5 },
  story_map: storyMap,
  error: null,
};

const profileCatalogItem = {
  id: 'cn_web_novel_v1',
  version: '3.0.0',
  display_name: '中文网文',
  description: '面向中文连载小说的均衡写作风格。',
  language: 'zh-CN',
  market: 'cn_web',
  publication_format: 'web_serial',
  primary_genre: 'xuanhuan',
  default_story_tags: ['progression_fantasy'],
  audience_rating: 'general',
  defaults: {
    pov: 'third_person_limited',
    tense: 'past',
    pace: 'slow_burn',
    prose_density: 'descriptive',
    dialogue_density: 'medium',
    chapter_word_range: [2200, 3200],
  },
};

const writingProfile = {
  project_id: project.project_id,
  content_language: 'zh-CN',
  target_market: 'cn_web',
  publication_format: 'web_serial',
  primary_genre: 'xuanhuan',
  story_tags: ['progression_fantasy'],
  audience_rating: 'general',
  content_warnings: ['graphic_violence'],
  writing_profile_id: 'cn_web_novel_v1',
  writing_profile_version: 3,
  writing_profile_overrides: { pov: 'third_person_limited', pace: 'slow_burn' },
  writing_profile_effective_from_chapter: 1,
  resolved_profile: profileCatalogItem,
  history: [{
    content_language: 'zh-CN',
    target_market: 'cn_web',
    publication_format: 'web_serial',
    primary_genre: 'xuanhuan',
    story_tags: ['progression_fantasy'],
    audience_rating: 'general',
    content_warnings: [],
    writing_profile_id: 'cn_web_novel_v1',
    writing_profile_version: 2,
    writing_profile_overrides: { tense: 'past' },
    writing_profile_effective_from_chapter: 1,
    change_reason: 'initial profile',
  }],
};

const chapterContract = {
  chapter: 5,
  title: '旧港档案',
  goal: '进入封存区并取得名册',
  plan_version: 2,
  locked: true,
  premise: project.premise,
  ending_direction: '守夜人保住城市的真实记忆。',
  volume: {
    volume_no: 1,
    title: volume.title,
    objective: volume.objective,
    key_results: volume.key_results,
  },
  arc_targets: ['推进守夜人的选择'],
  volume_kr_targets: [],
  milestone_targets: [],
  must_advance: ['找到封存名册'],
  must_resolve: [],
  must_include_entities: ['旧港档案员'],
  must_not_reveal: ['浓雾真正来源'],
  must_not_resolve: [],
  forbidden_state_changes: [],
  target_state: {
    knowledge_edges: ['主角知道名册被调包'],
    trust: 'broken',
  },
  success_criteria: ['拿到一条可验证线索', '至少完成一项 must_advance'],
  beats: [{ seq: 1, beat_type: 'turn', summary: '档案员主动交出假名册', constraint_level: 'hard' }],
};

const alignment = {
  chapter: 5,
  goal_score: 0.92,
  beat_score: 0.88,
  arc_score: 0.9,
  volume_score: 0.86,
  reveal_score: 1,
  divergence_score: 0.08,
  level: 'normal',
  decision: null,
  recommended_plan_action: 'keep',
  hard_reveal: false,
  detail: {},
  evidence_spans: [{ start: 0, end: 8, quote: '档案员主动交出假名册', label: 'proposal_evidence', confidence: 0.93 }],
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockLocalizedApi(page: Page): Promise<void> {
  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();

    if (path === '/v1/projects') return json(route, [project]);
    if (path === '/v1/projects/locale-book') return json(route, project);
    if (path === '/v1/catalogs/writing-profiles') return json(route, { items: [profileCatalogItem] });
    if (path === '/v1/catalogs/genres') return json(route, { items: [
      { id: 'xuanhuan', label: '玄幻' },
      { id: 'young_adult', label: 'Young Adult' },
      { id: 'speculative_fiction', label: 'Speculative Fiction' },
    ] });
    if (path === '/v1/locale-book/agent/sessions') return json(route, [finishedSession]);
    if (path === '/v1/locale-book/story-map') return json(route, storyMap);
    if (path === '/v1/locale-book/volumes') return json(route, [volume]);
    if (path === '/v1/locale-book/chapter-cards') return json(route, storyMap.chapters.map((chapter) => ({ ...chapter, beats: [] })));
    if (path === '/v1/locale-book/chapters/5/draft') {
      return json(route, {
        chapter: 5,
        revision_id: 'revision-5',
        revision_round: 1,
        draft_text: '档案室的灯整夜没有熄灭。',
        word_count: 14,
      });
    }
    if (path === '/v1/locale-book/chapters/5/contract') return json(route, chapterContract);
    if (path === '/v1/locale-book/chapters/5/alignment') return json(route, alignment);
    if (path === '/v1/locale-book/chapters/5/plan-versions') {
      return json(route, [{
        id: 'plan-version-1',
        chapter: 5,
        version: 1,
        payload: {},
        status: 'superseded',
        reason: 'agent_replan',
        created_at: '2026-08-30T08:00:00Z',
      }]);
    }
    if (path === '/v1/locale-book/pipeline/runs') return json(route, []);
    if (path === '/v1/locale-book/bible') return json(route, {
      content: '# Story Bible（as of ch.4）\n> 本文档由 `facts` 表确定性渲染，只读。共 1 条 canon facts。\n\n## 林默\n\n### power_rank\n- **境界**: 守夜人  *(ch.4)*\n',
      rendered_from: {},
      is_readonly: true,
    });
    if (path === '/v1/locale-book/state' && method === 'POST') {
      return json(route, {
        as_of_chapter: 4,
        power_ranks: { 林默: '守夜人' },
        knowledge_edges: [{
          knower_entity_id: '林默',
          secret_key: '雾港档案',
          knowledge_state: 'knows',
          secrecy_level: 'top_secret',
        }],
        timeline_events: ['林默发现广播会预告失踪者。'],
        item_ownership: { 防水名册: '林默' },
        gimmick_rules: [],
        numeric_facts: {},
      });
    }
    if (path === '/v1/locale-book/search/facts') {
      return json(route, { mode: 'bm25', hits: [{ id: 'fact-1', snippet: '名册不会被浓雾改写。', chapter: 4 }] });
    }
    if (path === '/v1/locale-book/reviews') {
      return json(route, [{
        candidate_id: 'candidate-1',
        fact_type: 'power_system',
        risk_tier: 'high',
        status: 'pending_review',
        reason: '这项变化会影响后续人物选择。',
        proposal_json: JSON.stringify({
          op: 'update',
          entity: '林默',
          new: { description: '拒绝遗忘并保留证据', canon_kind: 'power_rank' },
        }),
        source_chapter: 4,
      }]);
    }
    if (path === '/v1/locale-book/staging') return json(route, []);
    if (path === '/v1/locale-book/writing-profile') return json(route, writingProfile);
    if (path === '/v1/locale-book/memory/settings') {
      return json(route, {
        enable_l2: true,
        enable_vector: true,
        enable_l3: true,
        enable_summaries: true,
        l2_every_n_chapters: 2,
        l2_max_scenes_per_chapter: 8,
        vector_top_k: 6,
        summary_window_chapters: 5,
        embedding_provider: 'local-provider',
        embedding_model: 'local-model-v1',
        env_overrides: ['enable_l2'],
      });
    }
    if (path === '/v1/locale-book/memory/health') {
      const layer = { rows: 4, chapters: 4, completed_chapters: 4, coverage_pct: 100, details: {} };
      return json(route, {
        latest_chapter: 4,
        settings: {},
        layers: { l0: layer, l1: layer, l2: layer, summaries: layer },
        warnings: [
          '2 个最新 L0 文件缺失',
          'L0 FTS 尚有 3 章未索引',
          'L1 尚有 2 章未完成',
          'L1 有 4 条原子缺少可核验证据行',
          'L2 尚有 1 章待聚合',
        ],
      });
    }

    return json(route, { detail: `unmocked request: ${method} ${path}` }, 404);
  });
}

async function expectNoInternalCopy(page: Page, forbidden: string[]): Promise<void> {
  const visibleText = await page.locator('body').innerText();
  forbidden.forEach((value) => expect(visibleText, `页面不应显示内部文案“${value}”`).not.toContain(value));
}

test.beforeEach(async ({ page }) => {
  await mockLocalizedApi(page);
});

test('the Chinese landing page explains product concepts without unexplained English', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByText('AI 小说创作助手 · 体验版 v0.3')).toBeVisible();
  await expect(page.getByText('本地设定档案、确定性校验、按章节还原状态', { exact: false })).toBeVisible();
  await expect(page.locator('body')).toContainText('第 1 步');
  await expect(page.locator('body')).toContainText('人工审核');
  await expect(page.locator('body')).toContainText('数据结构版本');
  await expectNoInternalCopy(page, [
    'PASS', 'THE PIPELINE', 'STAGE 01', 'plan', 'recall', 'draft', 'canon',
    'validator', 'as-of', 'pipeline_run', 'THREE PILLARS', 'World Bible',
    'human_gate', 'auto_promote', 'DUAL MODE', 'FULL-SPECTRUM', 'START NOW',
    'tests green', 'made with',
  ]);
});

test('the shelf and project wizard use Chinese as the primary interface language', async ({ page }) => {
  await page.goto('/studio');

  await expect(page.getByText('你的创作书桌 · 1 本作品')).toBeVisible();
  await expect(page.locator('.shelf-project-card').getByText('简体中文 · zh-CN')).toBeVisible();
  await expect(page.locator('.shelf-project-card').getByText('已确认设定')).toBeVisible();
  await expectNoInternalCopy(page, ['STUDIO', 'YOUR WRITING DESK', 'MANUSCRIPT', 'NEXT', 'Canon']);

  await page.getByRole('button', { name: '＋ 新建一本书' }).click();
  await expect(page.getByText('新建作品 · 第 1/3 步')).toBeVisible();
  await expect(page.getByText('基础信息', { exact: true }).first()).toBeVisible();
  await expect(page.locator('#cp-language')).toContainText('简体中文');
  await page.getByRole('button', { name: '下一步 →' }).click();
  await expect(page.locator('#cp-primary-genre')).toContainText('青少年小说');
  await expect(page.locator('#cp-primary-genre')).toContainText('推想小说');
  await expect(page.locator('#cp-primary-genre')).not.toContainText('Young Adult');
  await expect(page.locator('#cp-primary-genre')).not.toContainText('Speculative Fiction');
  await expectNoInternalCopy(page, ['NEW PROJECT', 'BASICS', 'CONTENT LANGUAGE']);
});

test('planning and writing translate workflow enums and author-facing terminology', async ({ page }) => {
  await page.goto('/studio/locale-book/plan');

  await expect(page.getByRole('heading', { name: '规划室' })).toBeVisible();
  await expect(page.getByText('当前创作环节 · 定方向')).toBeVisible();
  await expect(page.getByText('作者选择驱动')).toBeVisible();
  await expect(page.getByLabel('设定确认方式')).toContainText('人工审核');
  await expect(page.getByText('人物变化线')).toBeVisible();
  await expect(page.getByText('已兑现')).toBeVisible();
  await expectNoInternalCopy(page, [
    'STORY CHECKPOINT', 'STEP 2', 'NOW', 'CHOICE-DRIVEN', 'Agent', 'checkpoint',
    'Canon', 'human_gate', 'guided', 'character', 'writing', 'met', 'OKR', 'KR',
  ]);

  await page.goto('/studio/locale-book/write/5');
  await expect(page.getByText('第 05 章 · 正文编辑中')).toBeVisible();
  await expect(page.getByText('草稿 · 设定待确认')).toBeVisible();
  await expect(page.locator('.contract-stamp').getByText('已锁定', { exact: true })).toBeVisible();
  await expect(page.getByText('转折')).toBeVisible();
  await expect(page.getByText('人物认知')).toBeVisible();
  await expect(page.getByText('信任状态')).toBeVisible();
  await expect(page.getByText('已经破裂')).toBeVisible();
  await expect(page.getByText('至少完成一项本章必须推进的内容')).toBeVisible();
  await expect(page.getByText('历史保留')).toBeVisible();
  await expect(page.getByText('创作助手重新规划')).toBeVisible();
  await page.getByRole('button', { name: /已发生 · 偏航检查/ }).click();
  await expect(page.getByRole('button', { name: '保持原计划' })).toBeVisible();
  await expect(page.getByText('设定提案证据')).toBeVisible();
  await expectNoInternalCopy(page, [
    'STORY SPINE', 'LIVE MANUSCRIPT', 'DRAFT /', 'CANON PENDING', 'LOCKED',
    'CHAPTER CONTRACT', 'VOL.', 'turn', 'knowledge_edges', 'keep', 'superseded',
    'broken', 'must_advance',
  ]);
});

test('real Agent decisions explain English actions and impact fields in Chinese', async ({ page }) => {
  await page.route('**/v1/locale-book/agent/sessions', (route) => json(route, [activeAgentSession]));
  await page.goto('/studio/locale-book/plan?session=session-locale-active');

  await expect(page.getByRole('heading', { name: '第 5 章已经规划完成，下一步怎么做？' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '生成第 5 章' })).toBeVisible();
  await expect(page.getByText('按已选章节路线生成 3 个候选稿，并预选表现最好的一稿。')).toBeVisible();
  await expect(page.getByText('后续规划约束 会遵守')).toBeVisible();
  await expect(page.getByText('候选状态 当前推荐稿')).toBeVisible();
  await expect(page.getByText('已确认设定 不会改变')).toBeVisible();
  await expect(page.getByText('后续计划 会调整')).toBeVisible();
  await expect(page.getByText('方案来源 从卷规划生成')).toBeVisible();
  await expect(page.getByText('本卷目标 保护港口记录（待验证）')).toBeVisible();
  await expect(page.getByText('此选择 可以撤回')).toBeVisible();
  await expectNoInternalCopy(page, [
    'Generate Chapter', 'Replan the future route', 'Finish for now',
    'uses_future_guardrails', 'canon_changed', 'reversible', 'future_plan_changed',
    'is_current_winner', 'source', 'volume_api', 'key_results', '"id"',
  ]);
});

test('world, review, and settings hide API values behind clear Chinese labels', async ({ page }) => {
  await page.goto('/studio/locale-book/world');
  await expect(page.getByText('已确认设定 · 查看某章状态')).toBeVisible();
  await expect(page.getByText('只读', { exact: true })).toBeVisible();
  await expect(page.locator('.world-bible-card')).toContainText('已确认设定总览（截至第 4 章）');
  await expect(page.locator('.world-bible-card')).toContainText('力量等级');
  await page.getByText('查看人物认知、世界规则与数值设定').click();
  await expect(page.getByRole('heading', { name: '人物认知' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '世界规则' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '数值设定' })).toBeVisible();
  await page.getByLabel('检索已确认设定').fill('名册');
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect(page.getByText('检索方式：关键词相关度检索')).toBeVisible();
  await expect(page.getByText('知情状态：已经知道', { exact: false })).toBeVisible();
  await expect(page.getByText('保密程度：最高机密', { exact: false })).toBeVisible();
  await expectNoInternalCopy(page, [
    'CANON', 'Canon', 'AS-OF', 'READ ONLY', 'CH.4', 'Story Bible', 'canon facts',
    'power_rank', 'bm25', 'knows', 'top_secret',
  ]);

  await page.goto('/studio/locale-book/review');
  await expect(page.getByText('人工审核 · 设定确认')).toBeVisible();
  await expect(page.getByText('力量体系')).toBeVisible();
  await expect(page.getByText('高风险')).toBeVisible();
  await expect(page.getByText('力量等级', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '确认这条设定' })).toBeVisible();
  await page.getByText('查看全部变更信息').click();
  await expect(page.getByText('变更方式')).toBeVisible();
  await expect(page.locator('.review-readable-change details').getByText('更新设定', { exact: true })).toBeVisible();
  await expectNoInternalCopy(page, [
    'HUMAN GATE', 'CANON PRESS', 'power_system', 'power_rank', 'pending_review',
    'canon_kind', '"op"', '"new"', '"entity"',
  ]);

  await page.goto('/studio/locale-book/settings');
  await expect(page.getByText('项目配置 · 仅影响本作品')).toBeVisible();
  await expect(page.getByText('简体中文 · zh-CN')).toBeVisible();
  await expect(page.getByText('中文网文读者')).toBeVisible();
  await expect(page.getByText('连载小说')).toBeVisible();
  await expect(page.getByText('场景记忆层', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('服务商：已配置')).toBeVisible();
  await expect(page.getByText('初始配置')).toBeVisible();
  await expect(page.getByText('有 2 个最新正文文件缺失')).toBeVisible();
  await expect(page.getByText('原始正文层的全文检索索引尚有 3 章未完成')).toBeVisible();
  await expect(page.getByText('证据层尚有 2 章未完成提取')).toBeVisible();
  await expect(page.getByText('证据层有 4 条记录缺少可核验的正文依据')).toBeVisible();
  await expect(page.getByText('场景记忆层尚有 1 章等待整理')).toBeVisible();
  await expectNoInternalCopy(page, [
    'ENGINE ROOM', 'PROJECT SCOPE', 'CONTENT LANGUAGE', 'TARGET MARKET', 'WRITING PROFILE',
    'EFFECTIVE FROM', 'VERSION HISTORY', 'cn_web', 'web_serial', 'third_person_limited',
    'slow_burn', 'enable_l2', 'L0', 'L1', 'L2', 'L3', 'Embedding',
    'local-provider', 'local-model-v1', 'initial profile', 'L0', 'L1', 'L2', 'FTS',
    '条原子', '环境变量',
  ]);
});

test('long Chinese labels reflow without page-level horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of [
    '/',
    '/studio',
    '/studio/locale-book/plan',
    '/studio/locale-book/write/5',
    '/studio/locale-book/world',
    '/studio/locale-book/review',
    '/studio/locale-book/settings',
  ]) {
    await page.goto(path);
    await expect(page.locator('body')).toBeVisible();
    const hasPageOverflow = await page.evaluate(() => (
      document.documentElement.scrollWidth > document.documentElement.clientWidth
    ));
    expect(hasPageOverflow, `${path} 不应产生页面级横向滚动`).toBe(false);
  }
});
