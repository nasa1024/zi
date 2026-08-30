import { ApiError } from '../api/client';

type TextMap = Readonly<Record<string, string>>;

const CONTENT_LANGUAGES: TextMap = {
  'zh-CN': '简体中文',
  'en-US': '英语（美国）',
  'en-GB': '英语（英国）',
};

const TARGET_MARKETS: TextMap = {
  cn_web: '中文网文读者',
  global_english: '全球英语读者',
  us: '美国读者',
  uk: '英国读者',
};

const PUBLICATION_FORMATS: TextMap = {
  web_serial: '连载小说',
  novel: '单本长篇',
};

const AUDIENCE_RATINGS: TextMap = {
  general: '大众',
  young_adult: '青少年适读',
  adult: '成人',
};

const GENRES: TextMap = {
  default: '通用',
  xianxia: '修仙',
  xuanhuan: '玄幻',
  wuxia: '武侠',
  kehuan: '科幻',
  dushi: '都市',
  lishi: '历史',
  yanqing: '言情',
  xuanyi: '悬疑',
  fantasy: '奇幻',
  romance: '爱情',
  mystery: '悬疑',
  thriller: '惊悚',
  science_fiction: '科幻',
  young_adult: '青少年小说',
  speculative_fiction: '推想小说',
};

const GENRE_DISPLAY_NAMES: TextMap = {
  'young adult': '青少年小说',
  'speculative fiction': '推想小说',
  'science fiction': '科幻',
};

const STORY_TAGS: TextMap = {
  progression_fantasy: '成长奇幻',
  litrpg: '游戏化冒险',
  cozy: '温馨治愈',
  cultivation: '修炼成长',
  system: '系统流',
  romance: '感情线',
  mystery: '悬疑',
  action: '动作冒险',
};

const WRITING_RULES: TextMap = {
  unspecified: '不限定',
  third_person_limited: '第三人称限知',
  first_person: '第一人称',
  third_person_omniscient: '第三人称全知',
  past: '过去时',
  present: '现在时',
  fast: '明快',
  balanced: '均衡',
  slow_burn: '慢热',
  accessible: '清晰易读',
  descriptive: '细腻描写',
  literary: '文学性',
  low: '较少',
  medium: '适中',
  high: '较多',
};

const CONTENT_WARNINGS: TextMap = {
  graphic_violence: '写实暴力',
  profanity: '粗口',
  sexual_content: '性内容',
  self_harm: '自伤相关',
};

const GOVERNANCE_MODES: TextMap = {
  human_gate: '人工审核',
  hybrid: '人机协作',
  auto_promote: '自动确认',
};

const INTERACTION_MODES: TextMap = {
  guided: '逐步选择',
  copilot: '协作创作',
  autonomous: '助手决定',
};

const AGENT_STAGES: TextMap = {
  bootstrap: '准备创作',
  story_plan: '确定全书方向',
  volume_plan: '规划当前卷',
  chapter_route: '安排近期章节',
  chapter_action: '确认章节写法',
  draft_choice: '选择候选正文',
  post_chapter: '核对本章结果',
  finished: '本轮已完成',
};

const SESSION_STATUSES: TextMap = {
  running: '进行中',
  awaiting_choice: '等待选择',
  finished: '已完成',
  completed: '已完成',
  paused: '已暂停',
  degraded: '已降级',
  error: '出现问题',
  failed: '执行失败',
  crashed: '异常中止',
  blocked: '已被阻止',
  canceled: '已取消',
  cancelled: '已取消',
  interrupted: '已中断',
  circuit_broken: '已暂停保护',
};

const PLAN_STATUSES: TextMap = {
  candidate: '候选方案',
  active: '当前执行',
  superseded: '历史保留',
  executed: '已经执行',
  abandoned: '已放弃',
  planned: '已规划',
  writing: '写作中',
  completed: '已完成',
  archived: '已归档',
  locked: '已锁定',
  open: '可修改',
  pending: '待处理',
};

const KEY_RESULT_STATUSES: TextMap = {
  pending: '待验证',
  met: '已兑现',
  partial: '部分兑现',
  missed: '未兑现',
};

const PLAN_ACTIONS: TextMap = {
  keep: '保持原计划',
  patch: '微调后续计划',
  replan: '重新规划后续',
  pause: '暂停并检查',
};

const DIVERGENCE_LEVELS: TextMap = {
  normal: '符合计划',
  minor: '轻微偏离',
  major: '明显偏离',
  severe: '严重偏离',
};

const ARC_TYPES: TextMap = {
  main: '主线情节弧',
  story: '故事主线',
  character: '人物变化线',
  relationship: '关系变化弧',
  romance: '感情发展弧',
  mystery: '悬疑线',
  world: '世界变化线',
  volume: '卷内情节弧',
  subplot: '支线情节弧',
  foreshadow: '伏笔线',
  other: '其他情节线',
};

const BEAT_TYPES: TextMap = {
  hook: '章末钩子',
  setup: '铺垫',
  turn: '转折',
  payoff_beat: '伏笔兑现',
  tension_point: '张力升级',
  inciting_incident: '诱发事件',
  rising_action: '冲突升级',
  complication: '变故',
  conflict: '正面冲突',
  turning_point: '转折',
  midpoint: '中段转折',
  climax: '高潮',
  resolution: '收束',
  cliffhanger: '悬念收尾',
  reveal: '信息揭示',
  payoff: '伏笔兑现',
  transition: '场景过渡',
  choice: '关键选择',
  setback: '受挫',
  discovery: '发现',
};

const TARGET_STATE_KEYS: TextMap = {
  power: '境界与力量',
  knowledge: '人物认知',
  timeline: '时间线事件',
  item: '物品归属',
  numeric: '数值设定',
  power_ranks: '力量等级',
  knowledge_edges: '人物认知',
  timeline_events: '时间线事件',
  item_ownership: '物品归属',
  gimmick_rules: '特殊规则',
  numeric_facts: '数值设定',
  character_state: '人物状态',
  relationship_state: '关系状态',
  location_state: '地点状态',
  route_step: '路线阶段',
  momentum: '推进状态',
  evidence_acquired: '已取得证据',
  trust: '信任状态',
  keywords: '验证关键词',
  route: '故事路线',
  chapter: '章节',
};

const FACT_TYPES: TextMap = {
  character: '人物设定',
  character_trait: '人物特征',
  character_state: '人物状态',
  appearance: '人物外观',
  location: '地点设定',
  item: '物品设定',
  relationship: '人物关系',
  timeline: '时间线',
  event: '事件',
  rule: '世界规则',
  power: '力量体系',
  power_system: '力量体系',
  power_rank: '力量等级',
  numeric: '数值设定',
  world_rule: '世界规则',
  foreshadow: '伏笔',
  foreshadowing: '伏笔',
  new_entity: '新人物或实体',
  entity: '人物或实体',
  misc: '其他设定',
  style: '文风设定',
  constraint: '设定禁忌',
  secret: '秘密',
  identity: '身份',
  knowledge: '人物认知',
  world: '世界设定',
};

const RISK_TIERS: TextMap = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
  critical: '关键风险',
};

const SEARCH_MODES: TextMap = {
  fts: '全文检索',
  fts5: '全文检索',
  full_text: '全文检索',
  keyword: '关键词检索',
  exact: '精确检索',
  vector: '语义检索',
  hybrid: '混合检索',
  bm25: '关键词相关度检索',
};

const MEMORY_SETTING_NAMES: TextMap = {
  enable_l2: '场景记忆层',
  enable_vector: '场景向量召回',
  enable_l3: '设定事件提案层',
  enable_summaries: '叙事摘要召回',
  l2_every_n_chapters: '场景记忆聚合间隔',
  l2_max_scenes_per_chapter: '单章场景上限',
  vector_top_k: '向量召回条数',
  summary_window_chapters: '章摘要窗口',
  embedding_provider: '向量服务商',
  embedding_model: '向量模型',
  NF_ENABLE_MEMORY_L2: '启用场景记忆层',
  NF_ENABLE_VECTOR_RETRIEVAL: '启用语义检索',
  NF_ENABLE_MEMORY_L3: '启用设定提案层',
  NF_ENABLE_SUMMARIES: '启用分层摘要',
  NF_EMBEDDING_PROVIDER: '向量服务商',
  NF_EMBEDDING_MODEL: '向量模型',
};

const WRITING_PROFILE_NAMES: TextMap = {
  cn_web_novel_v1: '中文网文通用风格',
  global_english_serial_v1: '英语连载通用风格',
  en_progression_serial_v1: '成长奇幻连载',
  en_litrpg_serial_v1: '游戏化成长连载',
  en_epic_fantasy_v1: '史诗奇幻',
  en_cozy_fantasy_v1: '温馨奇幻',
  en_grimdark_v1: '暗黑奇幻',
  en_romantasy_v1: '奇幻爱情',
  en_mystery_thriller_v1: '悬疑惊悚',
  en_ya_speculative_v1: '青少年推想小说',
};

const WRITING_PROFILE_DESCRIPTIONS: TextMap = {
  cn_web_novel_v1: '用明确冲突、价值变化和章末钩子推动中文连载。',
  global_english_serial_v1: '面向英语连载，兼顾清晰叙事、章节推进和追读动力。',
  en_progression_serial_v1: '突出可见成长、阶段目标、频繁进展反馈和推动追读的章末收束。',
  en_litrpg_serial_v1: '清晰呈现数值与规则反馈，形成有满足感的升级循环，避免过度解释系统。',
  en_epic_fantasy_v1: '承载大规模冲突，平衡群像与主线，并保留更丰富的场景描写。',
  en_cozy_fantasy_v1: '降低冲突压力，强调关系与日常回报，保持温暖舒缓的情绪。',
  en_grimdark_v1: '突出高代价选择与道德灰度，保持克制的黑暗感。',
  en_romantasy_v1: '让外部主线与感情线同步推进，并明确呈现关系变化节拍。',
  en_mystery_thriller_v1: '公平投放线索、控制信息披露、逐步升级悬念，并让反转有据可循。',
  en_ya_speculative_v1: '突出清晰的人物声音、身份与成长主题，并遵守适龄边界。',
};

const ALIGNMENT_EVIDENCE_LABELS: TextMap = {
  goal: '本章目标',
  must_advance: '必须推进',
  must_resolve: '必须解决',
  success_criteria: '验收条件',
  proposal_evidence: '设定提案证据',
  narrative_event: '叙事事件',
  beat: '情节节拍',
  arc: '情节弧',
  forbidden_reveal: '禁止提前揭示',
  state_change: '状态变化',
};

const PLAN_REASONS: TextMap = {
  agent: '创作助手生成',
  agent_replan: '创作助手重新规划',
  agent_post_chapter_replan: '根据本章结果重新规划',
  volume_api: '从卷规划生成',
  manual_edit: '作者手动修订',
  patch: '局部调整后续计划',
  replan: '完整重排后续计划',
  planning_api: '从规划室采用',
};

const SYSTEM_COPY_EXACT: TextMap = {
  'Choose the next step.': '请选择下一步',
  'Which whole-story direction should this novel follow?': '这本小说接下来采用哪一条全书路线？',
  'Which conflict and pacing plan should the next volume use?': '下一卷采用哪一种冲突与节奏方案？',
  'The chapter is saved. Continue, replan, or finish?': '本章已经保存，接下来继续、重排还是结束？',
  'The chapter has been reconciled with the plan. Continue, replan, or pause?': '本章已完成计划与实际核对，接下来保持、重排还是暂停？',
  'Generate three drafts from the selected chapter contract and preselect the strongest one.': '按已选章节路线生成 3 个候选稿，并预选表现最好的一稿。',
  'The current route is ready for drafting.': '当前路线已准备就绪，可以开始生成正文。',
  'Replan the future route': '重排后续路线',
  'Keep established facts and regenerate the future chapter window.': '保留已发生内容，重新生成后续章节路线。',
  'Use this when the current route no longer fits the story.': '适合当前路线已不再符合故事发展时使用。',
  'Finish for now': '暂时结束',
  'Save the session and all plans so work can resume from this point.': '保存当前会话和全部计划，下次可从这里继续。',
  'Continue to the next chapter': '继续下一章',
  'Accept and replan': '接受本章并重排后续',
  'Keep this chapter and replan future chapters from its actual outcome.': '保留本章正文，根据实际结果重新规划后续章节。',
  'End this writing session': '结束本次创作',
  'Save the checkpoint and continue later.': '保存当前进度，稍后继续。',
  'Pipeline-selected draft': '系统当前推荐稿',
  'Alternative draft available for manual selection': '可由你改选的备选稿',
  'Best balance of main objective, character growth, and future milestones': '主线目标、人物变化与后续关键节点最均衡',
  'A distinct emphasis on pace and conflict': '提供不同的节奏与冲突重心',
  'Best bridge from the whole-story plan': '与全书路线衔接最平衡',
  'An alternate volume rhythm': '可选的本卷节奏方案',
  'Best balance of movement, setup, and continuation hook': '推进、铺垫与章末悬念最均衡',
  'A distinct pacing emphasis': '提供不同的节奏倾向',
  'Generated by the unified planning service and applied as one transactional window.': '由规划室生成，采用后会一次更新这段章节路线。',
  'Offline FakeProvider: selected by option score and recommendation order': '离线测试模式：已按候选评分与推荐顺序选择。',
  '离线 FakeProvider：按候选评分与推荐顺序选择': '当前使用离线测试模式：已按候选评分与推荐顺序选择。',
  'No creative model is configured; selected by option score and recommendation order': '未配置创作模型：已按候选评分与推荐顺序选择。',
  '未配置创作模型：按候选评分与推荐顺序选择': '未配置创作模型：已按候选评分与推荐顺序选择。',
  'The model returned an invalid decision; selected deterministically': '模型返回的选择无效，已按候选评分自动选择。',
  '模型裁决格式无效：按候选评分回退': '模型返回的选择无效，已按候选评分自动选择。',
  'initial profile': '初始配置',
  project_create: '创建作品时生成',
};

const TARGET_STATE_VALUES: TextMap = {
  advanced: '已推进',
  unchanged: '保持不变',
  regressed: '出现回退',
  completed: '已完成',
  pending: '待达成',
  broken: '已经破裂',
  knows: '已经知道',
  suspects: '有所怀疑',
  unaware: '尚不知情',
  misinformed: '受到错误信息误导',
  public: '公开信息',
  open_secret: '少数人知情',
  secret: '秘密',
  top_secret: '最高机密',
};

function mapped(value: unknown, labels: TextMap, fallback: string): string {
  if (value == null || value === '') return fallback;
  return labels[String(value)] ?? fallback;
}

function containsChinese(value: string): boolean {
  return /[\u3400-\u9fff]/u.test(value);
}

export function contentLanguageLabel(value: unknown, includeCode = false): string {
  const code = value == null || value === '' ? 'zh-CN' : String(value);
  const label = CONTENT_LANGUAGES[code] ?? '其他语言';
  return includeCode ? `${label} · ${code}` : label;
}

export function contentUnitLabel(value: unknown): '字' | '词' {
  return String(value ?? '').toLowerCase().startsWith('en') ? '词' : '字';
}

export function targetMarketLabel(value: unknown): string {
  return mapped(value, TARGET_MARKETS, '其他读者市场');
}

export function publicationFormatLabel(value: unknown): string {
  return mapped(value, PUBLICATION_FORMATS, '其他发布形态');
}

export function audienceRatingLabel(value: unknown): string {
  return mapped(value, AUDIENCE_RATINGS, '未设置分级');
}

export function genreLabel(value: unknown, displayName?: string | null): string {
  const text = value == null ? '' : String(value);
  if (GENRES[text]) return GENRES[text];
  const suppliedName = displayName?.trim() ?? '';
  if (GENRE_DISPLAY_NAMES[suppliedName.toLowerCase()]) {
    return GENRE_DISPLAY_NAMES[suppliedName.toLowerCase()];
  }
  if (containsChinese(suppliedName)) return suppliedName;
  return containsChinese(text) ? text : '自定义类型';
}

export function storyTagLabel(value: unknown): string {
  if (value == null || value === '') return '未命名标签';
  const text = String(value);
  return STORY_TAGS[text] ?? (containsChinese(text) ? text : '自定义标签');
}

export function writingRuleLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return WRITING_RULES[text] ?? (containsChinese(text) ? text : '使用默认设置');
}

export function contentWarningLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return CONTENT_WARNINGS[text] ?? (containsChinese(text) ? text : '其他内容提醒');
}

export function governanceModeLabel(value: unknown): string {
  return mapped(value, GOVERNANCE_MODES, '人工审核');
}

export function interactionModeLabel(value: unknown): string {
  return mapped(value, INTERACTION_MODES, '逐步选择');
}

export function agentStageLabel(value: unknown): string {
  return mapped(value, AGENT_STAGES, '处理创作步骤');
}

export function sessionStatusLabel(value: unknown): string {
  return mapped(value, SESSION_STATUSES, '状态待确认');
}

export function planStatusLabel(value: unknown): string {
  return mapped(value, PLAN_STATUSES, '状态待确认');
}

export function volumeStatusLabel(value: unknown): string {
  return mapped(value, PLAN_STATUSES, '状态待确认');
}

export function keyResultStatusLabel(value: unknown): string {
  return mapped(value, KEY_RESULT_STATUSES, '待验证');
}

export function planActionLabel(value: unknown): string {
  return mapped(value, PLAN_ACTIONS, '等待人工判断');
}

export function divergenceLevelLabel(value: unknown): string {
  return mapped(value, DIVERGENCE_LEVELS, '等待检查');
}

export function arcTypeLabel(value: unknown): string {
  return mapped(value, ARC_TYPES, '故事情节弧');
}

export function beatTypeLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return BEAT_TYPES[text] ?? (containsChinese(text) ? text : '故事节拍');
}

export function targetStateKeyLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return TARGET_STATE_KEYS[text] ?? (containsChinese(text) ? text : '目标状态');
}

export function targetStateValueLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  if (TARGET_STATE_VALUES[text]) return TARGET_STATE_VALUES[text];
  if (containsChinese(text) || /^[\d.+%/-]+$/.test(text)) return text;
  return /^[a-z][a-z0-9_]*$/i.test(text) ? '状态已记录' : text;
}

export function factTypeLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return FACT_TYPES[text] ?? (containsChinese(text) ? text : '设定事实');
}

export function riskTierLabel(value: unknown): string {
  return mapped(value, RISK_TIERS, '风险待判断');
}

export function searchModeLabel(value: unknown): string {
  return mapped(value, SEARCH_MODES, '综合检索');
}

export function memorySettingLabel(value: unknown): string {
  return mapped(value, MEMORY_SETTING_NAMES, '系统环境设置');
}

export function writingProfileLabel(
  identifier: unknown,
  displayName?: string | null,
): string {
  const id = identifier == null ? '' : String(identifier);
  if (WRITING_PROFILE_NAMES[id]) return WRITING_PROFILE_NAMES[id];
  if (displayName?.trim() && containsChinese(displayName)) return displayName.trim();
  return '自定义写作风格';
}

export function writingProfileDescription(
  identifier: unknown,
  source?: string | null,
): string {
  const id = identifier == null ? '' : String(identifier);
  if (WRITING_PROFILE_DESCRIPTIONS[id]) return WRITING_PROFILE_DESCRIPTIONS[id];
  if (source?.trim() && containsChinese(source)) return source.trim();
  return '此风格会用于未来章节的规划、起草和质量检查。';
}

export function alignmentEvidenceLabel(value: unknown): string {
  const text = value == null ? '' : String(value);
  return ALIGNMENT_EVIDENCE_LABELS[text] ?? (containsChinese(text) ? text : '正文证据');
}

export function planReasonLabel(value: unknown, status?: unknown): string {
  const text = value == null ? '' : String(value).trim();
  if (PLAN_REASONS[text]) return PLAN_REASONS[text];
  if (containsChinese(text)) return text;
  return status === 'active' ? '系统生成的当前版本' : '历史计划调整';
}

export function systemCopyText(value: unknown, fallback = '创作信息已记录'): string {
  const text = value == null ? '' : String(value).trim();
  if (!text) return fallback;
  if (SYSTEM_COPY_EXACT[text]) return SYSTEM_COPY_EXACT[text];

  const generateChapter = text.match(/^Generate Chapter (\d+)$/);
  if (generateChapter) return `生成第 ${generateChapter[1]} 章`;

  const draft = text.match(/^Draft (\d+)$/);
  if (draft) return `候选稿 ${draft[1]}`;

  const futureRoute = text.match(/^Which future route should begin with Chapter (\d+)\?$/);
  if (futureRoute) return `从第 ${futureRoute[1]} 章开始，后续路线选择哪一套？`;

  const plannedChapter = text.match(/^Chapter (\d+) is planned\. What should happen next\?$/);
  if (plannedChapter) return `第 ${plannedChapter[1]} 章已经规划完成，下一步怎么做？`;

  const draftChoice = text.match(/^Which draft should Chapter (\d+|\?) use\?$/);
  if (draftChoice) return `第 ${draftChoice[1]} 章采用哪一稿？`;

  const routeWithDivergence = text.match(
    /^Keep the current route and use the latest world state\.(?: Current divergence: ([\d.]+)%\.)?$/,
  );
  if (routeWithDivergence) {
    return routeWithDivergence[1]
      ? `保持当前路线，使用最新的已确认设定进入下一章。当前偏离 ${routeWithDivergence[1]}%。`
      : '保持当前路线，使用最新的已确认设定进入下一章。';
  }

  if (/^(?:模型裁决失败，已确定性回退|Model decision failed; selected deterministically)\s*[:：]\s*[A-Za-z][A-Za-z0-9_.]*$/.test(text)) {
    return '智能推荐暂不可用：已按候选评分自动选择。';
  }

  return text
    .replace(/\bhuman_gate\b/gi, '人工审核')
    .replace(/\bauto_promote\b/gi, '自动确认')
    .replace(/([\u3400-\u9fff])\s*\bmust_advance\b/gi, '$1本章必须推进的内容')
    .replace(/\bmust_advance\b/gi, '本章必须推进的内容')
    .replace(/\bmust_resolve\b/gi, '本章必须解决的内容')
    .replace(/\bmust_include_entities\b/gi, '必须出场的人物或事物')
    .replace(/\bmust_not_reveal\b/gi, '不得提前揭示的内容')
    .replace(/\bmust_not_resolve\b/gi, '不得提前解决的内容')
    .replace(/\bforbidden_state_changes\b/gi, '禁止发生的状态变化')
    .replace(/\bsuccess_criteria\b/gi, '验收条件')
    .replace(/([\u3400-\u9fff])\s*\bcheckpoint\b/gi, '$1当前进度')
    .replace(/\bcheckpoint\b/gi, '当前进度')
    .replace(/\bcanon\b/gi, '已确认设定')
    .replace(/\bAgent\b/g, '创作助手');
}

export function memoryWarningText(value: unknown): string {
  const raw = value == null ? '' : String(value).trim();
  if (!raw) return '记忆层存在需要处理的问题。';

  const exactRules: Array<[RegExp, string]> = [
    [/^(\d+)\s*个最新\s*L0\s*文件缺失$/i, '有 $1 个最新正文文件缺失'],
    [/^L0\s+FTS\s+尚有\s+(\d+)\s*章未索引$/i, '原始正文层的全文检索索引尚有 $1 章未完成'],
    [/^L1\s+尚有\s+(\d+)\s*章未完成$/i, '证据层尚有 $1 章未完成提取'],
    [/^L1\s+有\s+(\d+)\s*条原子缺少可核验证据行$/i, '证据层有 $1 条记录缺少可核验的正文依据'],
    [/^L2\s+尚有\s+(\d+)\s*章待聚合$/i, '场景记忆层尚有 $1 章等待整理'],
  ];
  const exact = exactRules.find(([pattern]) => pattern.test(raw));
  if (exact) return raw.replace(exact[0], exact[1]);

  const localized = systemCopyText(raw)
    .replace(/\bL0\b/gi, '原始正文层')
    .replace(/\bL1\b/gi, '证据层')
    .replace(/\bL2\b/gi, '场景记忆层')
    .replace(/\bL3\b/gi, '设定提案层')
    .replace(/\bFTS5?\b/gi, '全文检索索引')
    .replace(/条原子/g, '条证据记录')
    .replace(/孤儿记录/g, '无关联记录')
    .replace(/模型或元数据已过期/g, '所用模型或记录信息已过期');
  return /[A-Za-z]/.test(localized)
    ? '记忆层存在需要处理的问题，请刷新或重建后再试。'
    : localized;
}

function knownSystemError(raw: string): string | null {
  if (/draft\s*失败\s*[:：]\s*所有候选均无产出/i.test(raw)) {
    return '正文生成失败：所有候选稿都没有生成内容，请重试或检查模型配置。';
  }
  if (/wrong_output_language/i.test(raw)) {
    return '生成内容与作品语言不一致，请重新生成。';
  }
  if (/writing_profile_constraints/i.test(raw)) {
    return '正文未满足当前写作风格的硬性要求，请调整后重试。';
  }

  const stageMatch = raw.match(/^([a-z][a-z0-9_]*)\s*失败(?:\s*[:：]\s*)?/i);
  if (!stageMatch) return null;
  const stages: TextMap = {
    recall: '读取前文',
    plan: '核对章节路线',
    candidates: '生成候选稿',
    draft: '生成正文',
    check: '检查设定冲突',
    plan_alignment: '核对故事路线',
    gate: '检查正文质量',
    plan_reconcile: '更新后续计划',
    settle: '更新已确认设定',
    quality: '评估文本表现',
  };
  const stage = stages[stageMatch[1].toLowerCase()];
  return stage ? `${stage}失败，请稍后重试。` : null;
}

export function userFacingError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return '没有权限完成此操作，请检查访问权限。';
    if (error.status === 404) return '请求的内容暂时不可用，请刷新后重试。';
    if (error.status === 409) return fallback;
    if (error.status === 422) return '提交内容不完整或格式不正确，请检查后重试。';
    if (error.status === 429) return '操作过于频繁，请稍后再试。';
    if (error.status >= 500) return '服务暂时开小差，请稍后再试。';
  }

  if (error instanceof Error) {
    const raw = error.message.trim();
    if (/failed to fetch|networkerror|load failed/i.test(raw)) {
      return '网络连接失败，请检查服务状态后重试。';
    }
    const localized = knownSystemError(raw);
    if (localized) return localized;
    const exposesRoute = /(?:not found|request failed|http\s*\d+|\/v\d+\/)/i.test(raw);
    const exposesIdentifier = /\b[a-z]+(?:_[a-z0-9]+)+\b/i.test(raw);
    const exposesEnglish = /[A-Za-z]/.test(raw);
    if (raw && containsChinese(raw) && !exposesRoute && !exposesIdentifier && !exposesEnglish) return raw;
  }

  return fallback;
}
