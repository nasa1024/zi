import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '../../api/client';
import type {
  ContentLanguage,
  GenreCatalogItem,
  PlotSetting,
  ProjectCreateRequest,
  ProjectResponse,
  WritingProfileCatalogItem,
} from '../../api/types';
import {
  audienceRatingLabel,
  contentLanguageLabel,
  contentWarningLabel,
  genreLabel,
  publicationFormatLabel,
  storyTagLabel,
  targetMarketLabel,
  userFacingError,
  writingProfileDescription,
  writingProfileLabel,
  writingRuleLabel,
} from '../../utils/uiText';
import SettingOptionCards from './SettingOptionCards';

export interface CreateProjectModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (project: ProjectResponse) => void;
}

type WizardStep = 0 | 1 | 2;
type Mode = 'manual' | 'generate';
type TextKey =
  | 'premise'
  | 'world_setting'
  | 'power_system'
  | 'protagonist'
  | 'main_conflict'
  | 'opening_hook'
  | 'target_audience'
  | 'tone';
type FieldKind = 'text' | 'textarea' | 'combo' | 'select';

interface FieldDef {
  key: TextKey;
  label: string;
  kind: FieldKind;
  options?: string[];
  optionsFor?: (genre: string) => string[];
}

interface LanguageDefaults {
  market: string;
  format: string;
  genre: string;
}

const STEP_LABELS = ['基础信息', '语言 · 市场 · 风格', '情节设定'];
const LANGUAGE_DEFAULTS: Record<ContentLanguage, LanguageDefaults> = {
  'zh-CN': { market: 'cn_web', format: 'web_serial', genre: 'xuanhuan' },
  'en-US': { market: 'global_english', format: 'web_serial', genre: 'fantasy' },
  'en-GB': { market: 'global_english', format: 'web_serial', genre: 'fantasy' },
};

const MARKET_OPTIONS: Record<string, { value: string; disabled?: boolean }[]> = {
  'zh-CN': [{ value: 'cn_web' }],
  'en-US': [
    { value: 'global_english' },
    { value: 'us', disabled: true },
    { value: 'uk', disabled: true },
  ],
};

const POWER_SYSTEM_OPTIONS: Record<string, string[]> = {
  xianxia: ['炼气境', '剑道', '丹道', '体修', '神魂', '符箓'],
  xuanhuan: ['斗气', '武魂', '魂力', '血脉', '神格'],
  wuxia: ['内功', '真气', '武学境界'],
  kehuan: ['基因锁', '机甲等级', '念能力', '进化阶段'],
  dushi: ['异能', '古武', '觉醒者等级'],
  lishi: ['武将统帅', '谋略', '兵法'],
  yanqing: ['灵力', '情丝', '心境'],
  xuanyi: ['推理', '侧写', '线索链'],
};
const GENERIC_POWER_SYSTEMS = ['境界', '等级', '段位', '阶位'];
const powerSystemOptions = (genre: string): string[] => [
  ...(POWER_SYSTEM_OPTIONS[genre] ?? []),
  ...GENERIC_POWER_SYSTEMS,
];

const CORE_FIELDS: FieldDef[] = [
  { key: 'premise', label: '一句话设定', kind: 'text' },
  { key: 'power_system', label: '力量体系', kind: 'combo', optionsFor: powerSystemOptions },
  { key: 'protagonist', label: '主角设定', kind: 'textarea' },
  {
    key: 'main_conflict',
    label: '主线冲突',
    kind: 'combo',
    options: [
      '复仇雪耻', '逆袭崛起', '争霸天下', '守护苍生', '夺宝奇遇', '家族复兴',
      '探寻真相', '宿命对决', '守护挚爱', '求道长生', '门派恩怨', '王朝更替',
    ],
  },
];
const ADVANCED_FIELDS: FieldDef[] = [
  { key: 'world_setting', label: '世界观', kind: 'textarea' },
  { key: 'opening_hook', label: '开篇钩子', kind: 'textarea' },
  { key: 'target_audience', label: '目标读者', kind: 'select', options: ['男频', '女频', '全龄向', '不限'] },
  {
    key: 'tone',
    label: '基调',
    kind: 'combo',
    options: ['热血', '爽快', '轻松诙谐', '硬核严肃', '暗黑', '治愈', '沉郁', '悬疑紧张', '智斗', '燃向'],
  },
];

const RANK_TEMPLATES = [
  { label: '经典修仙 · 九境', ranks: ['炼气', '筑基', '金丹', '元婴', '化神', '炼虚', '合体', '大乘', '渡劫'] },
  { label: '斗气大陆', ranks: ['斗之气', '斗者', '斗师', '大斗师', '斗灵', '斗王', '斗皇', '斗宗', '斗尊', '斗圣', '斗帝'] },
  { label: '武魂 · 魂师', ranks: ['魂士', '魂师', '大魂师', '魂尊', '魂宗', '魂王', '魂帝', '魂圣', '封号斗罗'] },
  { label: '通用 · 凡人到仙', ranks: ['凡人', '后天', '先天', '宗师', '大宗师', '半步封王', '封王', '封帝'] },
];

const SENTINEL_PREFIXES = ['（示例）', '(Example)', '[Example]'];
const CONTENT_WARNING_OPTIONS = ['graphic_violence', 'profanity', 'sexual_content', 'self_harm'];
const KNOWN_STORY_TAG_VALUES = [
  'progression_fantasy',
  'litrpg',
  'cozy',
  'cultivation',
  'system',
  'romance',
  'mystery',
  'action',
];
const STORY_TAG_VALUE_BY_LABEL = new Map(
  KNOWN_STORY_TAG_VALUES.map((value) => [storyTagLabel(value), value]),
);

function emptyDraft(genre: string): PlotSetting {
  return { genre, power_ranks: [] };
}

function stripSentinel(setting: PlotSetting): PlotSetting {
  const output = { ...setting } as Record<string, unknown>;
  Object.keys(output).forEach((key) => {
    const value = output[key];
    if (
      typeof value === 'string'
      && SENTINEL_PREFIXES.some((prefix) => value.startsWith(prefix))
    ) {
      output[key] = null;
    }
  });
  return output as unknown as PlotSetting;
}

function parseTags(value: string): string[] {
  return Array.from(new Set(
    value
      .split(/[,，]/)
      .map((tag) => tag.trim())
      .filter(Boolean),
  ));
}

function parseStoryTags(value: string): string[] {
  return parseTags(value).map((tag) => STORY_TAG_VALUE_BY_LABEL.get(tag) ?? tag);
}

function catalogGenreLabel(genre: GenreCatalogItem): string {
  const mapped = genreLabel(genre.id);
  return mapped === '自定义类型' ? genreLabel(genre.id, genre.label) : mapped;
}

function catalogProfileLabel(profile: WritingProfileCatalogItem | null): string {
  if (!profile) return '等待写作风格';
  const mapped = writingProfileLabel(profile.id);
  return mapped === '自定义写作风格'
    ? writingProfileLabel(profile.id, profile.name)
    : mapped;
}

function errorMessage(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}

export default function CreateProjectModal({
  open,
  onClose,
  onCreated,
}: CreateProjectModalProps): JSX.Element | null {
  const defaults = LANGUAGE_DEFAULTS['zh-CN'];
  const [step, setStep] = useState<WizardStep>(0);
  const [name, setName] = useState('');
  const [contentLanguage, setContentLanguage] = useState<ContentLanguage>('zh-CN');
  const [targetMarket, setTargetMarket] = useState(defaults.market);
  const [publicationFormat, setPublicationFormat] = useState(defaults.format);
  const [primaryGenre, setPrimaryGenre] = useState(defaults.genre);
  const [storyTags, setStoryTags] = useState<string[]>([]);
  const [storyTagsText, setStoryTagsText] = useState('');
  const [audienceRating, setAudienceRating] = useState('general');
  const [contentWarnings, setContentWarnings] = useState<string[]>([]);
  const [profileId, setProfileId] = useState('');
  const [profileOverrides, setProfileOverrides] = useState<Record<string, unknown>>({});
  const [showProfileAdvanced, setShowProfileAdvanced] = useState(false);

  const [profiles, setProfiles] = useState<WritingProfileCatalogItem[]>([]);
  const [genres, setGenres] = useState<GenreCatalogItem[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogNonce, setCatalogNonce] = useState(0);

  const [mode, setMode] = useState<Mode>('generate');
  const [draft, setDraft] = useState<PlotSetting>(() => emptyDraft(defaults.genre));
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [edited, setEdited] = useState(false);
  const [hints, setHints] = useState('');
  const [candidates, setCandidates] = useState<PlotSetting[]>([]);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  const [lockedKeys, setLockedKeys] = useState<Set<string>>(new Set());
  const [generating, setGenerating] = useState(false);
  const [rerollKey, setRerollKey] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedProfile = useMemo(
    () => profiles.find((profile) => profile.id === profileId) ?? null,
    [profileId, profiles],
  );

  useEffect(() => {
    if (!open) return;
    const initial = LANGUAGE_DEFAULTS['zh-CN'];
    setStep(0);
    setName('');
    setContentLanguage('zh-CN');
    setTargetMarket(initial.market);
    setPublicationFormat(initial.format);
    setPrimaryGenre(initial.genre);
    setStoryTags([]);
    setStoryTagsText('');
    setAudienceRating('general');
    setContentWarnings([]);
    setProfileId('');
    setProfileOverrides({});
    setShowProfileAdvanced(false);
    setMode('generate');
    setDraft(emptyDraft(initial.genre));
    setShowAdvanced(false);
    setEdited(false);
    setHints('');
    setCandidates([]);
    setSelectedIdx(null);
    setLockedKeys(new Set());
    setGenerating(false);
    setRerollKey(null);
    setDegraded(false);
    setGenError(null);
    setSubmitting(false);
    setError(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setCatalogLoading(true);
    setCatalogError(null);
    void Promise.all([
      api.listWritingProfiles(contentLanguage, publicationFormat, targetMarket),
      api.listGenres(contentLanguage, targetMarket),
    ]).then(([nextProfiles, nextGenres]) => {
      if (cancelled) return;
      setProfiles(nextProfiles);
      setGenres(nextGenres);
      setProfileId((current) => (
        nextProfiles.some((profile) => profile.id === current)
          ? current
          : (nextProfiles[0]?.id ?? '')
      ));
      setPrimaryGenre((current) => (
        nextGenres.some((genre) => genre.id === current)
          ? current
          : (nextGenres[0]?.id ?? LANGUAGE_DEFAULTS[contentLanguage].genre)
      ));
    }).catch((loadError: unknown) => {
      if (cancelled) return;
      setProfiles([]);
      setGenres([]);
      setCatalogError(errorMessage(loadError, '写作配置目录加载失败'));
    }).finally(() => {
      if (!cancelled) setCatalogLoading(false);
    });
    return () => { cancelled = true; };
  }, [catalogNonce, contentLanguage, open, publicationFormat, targetMarket]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting && !generating) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [generating, onClose, open, submitting]);

  if (!open) return null;

  const busy = submitting || generating;
  const trimmedName = name.trim();
  const catalogReady = !catalogLoading && !catalogError && Boolean(profileId) && Boolean(primaryGenre);
  const showEditor = mode === 'manual' || selectedIdx !== null;
  const showFieldTools = mode === 'generate' && selectedIdx !== null;
  const isEnglish = contentLanguage.startsWith('en');
  const profileRules = { ...(selectedProfile?.prompt_rules ?? {}), ...profileOverrides };
  const chapterRange = selectedProfile?.quality_rules.chapter_word_range;

  const clearCandidates = (genre = primaryGenre) => {
    setCandidates([]);
    setSelectedIdx(null);
    setLockedKeys(new Set());
    setEdited(false);
    setDegraded(false);
    setGenError(null);
    setDraft(emptyDraft(genre));
  };

  const confirmCandidateReset = (message: string): boolean => (
    (candidates.length === 0 && !edited) || window.confirm(message)
  );

  const handleLanguageChange = (language: ContentLanguage) => {
    if (language === contentLanguage) return;
    if (!confirmCandidateReset('语言变更会清空已经生成或填写的设定，并重新加载兼容风格。确定吗？')) return;
    const next = LANGUAGE_DEFAULTS[language];
    setContentLanguage(language);
    setTargetMarket(next.market);
    setPublicationFormat(next.format);
    setPrimaryGenre(next.genre);
    setStoryTags([]);
    setStoryTagsText('');
    setAudienceRating('general');
    setContentWarnings([]);
    setProfileId('');
    setProfileOverrides({});
    clearCandidates(next.genre);
  };

  const handleMarketChange = (market: string) => {
    if (!confirmCandidateReset('市场变更会清空已经生成或填写的设定。确定吗？')) return;
    setTargetMarket(market);
    setProfileId('');
    setPrimaryGenre('');
    setStoryTags([]);
    setStoryTagsText('');
    setProfileOverrides({});
    clearCandidates('');
  };

  const handleGenreChange = (genre: string) => {
    if (!confirmCandidateReset('题材变更会清空已经生成或填写的设定。确定吗？')) return;
    setPrimaryGenre(genre);
    clearCandidates(genre);
  };

  const handleProfileChange = (id: string) => {
    if (!confirmCandidateReset('风格变更会清空已经生成或填写的设定。确定吗？')) return;
    const profile = profiles.find((item) => item.id === id);
    setProfileId(id);
    setProfileOverrides({});
    const nextStoryTags = profile?.default_story_tags ?? profile?.story_tags ?? [];
    setStoryTags(nextStoryTags);
    setStoryTagsText(nextStoryTags.map(storyTagLabel).join(', '));
    clearCandidates(primaryGenre);
  };

  const setOverride = (key: string, value: string) => {
    setProfileOverrides((current) => {
      const next = { ...current };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  };

  const setField = (key: TextKey, value: string) => {
    setDraft((current) => ({ ...current, genre: primaryGenre, [key]: value }));
    setEdited(true);
  };

  const setRanksText = (value: string) => {
    const ranks = value.split('\n').map((rank) => rank.trim()).filter(Boolean);
    setDraft((current) => ({ ...current, genre: primaryGenre, power_ranks: ranks }));
    setEdited(true);
  };

  const toggleLock = (key: string) => {
    setLockedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const writingConfig = () => ({
    content_language: contentLanguage,
    target_market: targetMarket,
    publication_format: publicationFormat,
    primary_genre: primaryGenre,
    story_tags: storyTags,
    audience_rating: audienceRating,
    content_warnings: contentWarnings,
    writing_profile_id: selectedProfile?.id ?? profileId,
    writing_profile_overrides: profileOverrides,
  });

  const handleGenerate = async () => {
    if (generating) return;
    if (
      candidates.length > 0
      && edited
      && !window.confirm('重新生成会替换当前候选，已编辑的内容将丢失。确定吗？')
    ) return;
    setGenerating(true);
    setGenError(null);
    try {
      const lockedDraft = { ...draft, genre: primaryGenre };
      const response = await api.suggestSetting({
        name: trimmedName || null,
        genre: primaryGenre,
        hints: hints.trim() || null,
        n: 3,
        locked: lockedKeys.size > 0 ? lockedDraft : null,
        locked_fields: Array.from(lockedKeys),
        ...writingConfig(),
      });
      setCandidates(response.candidates);
      setDegraded(response.degraded);
      setSelectedIdx(null);
      setEdited(false);
    } catch (generateError) {
      setGenError(errorMessage(generateError, '生成失败'));
    } finally {
      setGenerating(false);
    }
  };

  const handlePick = (index: number) => {
    if (
      selectedIdx !== null
      && edited
      && !window.confirm('切换候选会丢弃当前编辑的内容。确定吗？')
    ) return;
    const candidate = candidates[index];
    setSelectedIdx(index);
    setDraft(stripSentinel({ ...candidate, genre: primaryGenre, power_ranks: candidate.power_ranks ?? [] }));
    setShowAdvanced(true);
    setEdited(false);
  };

  const handleReroll = async (key: string) => {
    if (generating || rerollKey) return;
    setRerollKey(key);
    setGenError(null);
    try {
      const currentValue = (draft as unknown as Record<string, unknown>)[key];
      const response = await api.suggestSetting({
        genre: primaryGenre,
        locked: { ...draft, genre: primaryGenre },
        locked_fields: Array.from(lockedKeys),
        field: key,
        avoid: typeof currentValue === 'string' && currentValue ? [currentValue] : [],
        n: 1,
        ...writingConfig(),
      });
      const next = response.candidates[0];
      if (next) {
        const value = (next as unknown as Record<string, unknown>)[key];
        setDraft((current) => ({ ...current, [key]: value as never }));
        setEdited(true);
      }
    } catch (rerollError) {
      setGenError(errorMessage(rerollError, '重新生成失败'));
    } finally {
      setRerollKey(null);
    }
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (step !== 2 || busy || !catalogReady) return;
    setSubmitting(true);
    setError(null);
    const cleaned = stripSentinel({ ...draft, genre: primaryGenre });
    const request: ProjectCreateRequest = {
      name: trimmedName,
      genre: primaryGenre,
      power_system: cleaned.power_system ?? null,
      setting: cleaned,
      ...writingConfig(),
    };
    try {
      onCreated(await api.createProject(request));
    } catch (createError) {
      setError(errorMessage(createError, '创建项目失败'));
      setSubmitting(false);
    }
  };

  const renderField = ({ key, label, kind, options, optionsFor }: FieldDef): JSX.Element => {
    const locked = lockedKeys.has(key);
    const value = (draft[key] as string | null | undefined) ?? '';
    const englishKind: FieldKind = isEnglish && (kind === 'combo' || kind === 'select') ? 'text' : kind;
    const values = optionsFor ? optionsFor(primaryGenre) : (options ?? []);
    const dataListId = `dl-${key}`;
    return (
      <div className="nf-field full" key={key}>
        <div className="setting-field-head">
          <label htmlFor={`cp-${key}`}>{label}</label>
          {showFieldTools && (
            <span className="setting-field-tools">
              <button
                type="button"
                className={`nf-btn-sm${locked ? ' on' : ''}`}
                aria-pressed={locked}
                aria-label={`${locked ? '已锁定' : '锁定'}：${label}`}
                title={locked ? '已锁定（重新生成时保持不变）' : '锁定此字段'}
                onClick={() => toggleLock(key)}
                disabled={busy}
              >
                {locked ? '🔒' : '🔓'}
              </button>
              <button
                type="button"
                className="nf-btn-sm"
                aria-label={`重新生成：${label}`}
                title="重新生成此字段"
                onClick={() => void handleReroll(key)}
                disabled={busy || rerollKey !== null}
              >
                {rerollKey === key ? <span className="nf-spin" /> : '✨'}
              </button>
            </span>
          )}
        </div>
        {englishKind === 'textarea' && (
          <textarea id={`cp-${key}`} className="nf-textarea" rows={2} value={value} onChange={(event) => setField(key, event.target.value)} disabled={busy} />
        )}
        {englishKind === 'text' && (
          <input id={`cp-${key}`} className="nf-input" type="text" value={value} onChange={(event) => setField(key, event.target.value)} disabled={busy} />
        )}
        {englishKind === 'combo' && (
          <>
            <input id={`cp-${key}`} className="nf-input" type="text" list={dataListId} placeholder="可下拉选择，也可自行输入" value={value} onChange={(event) => setField(key, event.target.value)} disabled={busy} />
            <datalist id={dataListId}>{values.map((item) => <option key={item} value={item} />)}</datalist>
          </>
        )}
        {englishKind === 'select' && (
          <select id={`cp-${key}`} className="nf-select" value={value} onChange={(event) => setField(key, event.target.value)} disabled={busy}>
            <option value="">（选填，请选择）</option>
            {value && !values.includes(value) && <option value={value}>{value}</option>}
            {values.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        )}
      </div>
    );
  };

  const renderRanks = (): JSX.Element => (
    <div className="nf-field full">
      <div className="setting-field-head">
        <label htmlFor="cp-ranks">力量/进阶阶梯（每行一级，由弱到强）</label>
        {showFieldTools && (
          <button type="button" className="nf-btn-sm" aria-label="重新生成：进阶阶梯" onClick={() => void handleReroll('power_ranks')} disabled={busy || rerollKey !== null}>
            {rerollKey === 'power_ranks' ? <span className="nf-spin" /> : '✨'}
          </button>
        )}
      </div>
      {!isEnglish && (
        <select
          className="nf-select rank-template"
          value=""
          aria-label="套用力量阶梯模板"
          onChange={(event) => {
            const template = RANK_TEMPLATES.find((item) => item.label === event.target.value);
            if (template) setRanksText(template.ranks.join('\n'));
          }}
          disabled={busy}
        >
          <option value="">套用模板…（也可在下方手动输入）</option>
          {RANK_TEMPLATES.map((template) => <option key={template.label} value={template.label}>{template.label}（{template.ranks.length} 阶）</option>)}
        </select>
      )}
      <textarea id="cp-ranks" className="nf-textarea" rows={3} placeholder={isEnglish ? 'Novice\nAdept\nMaster' : '炼气\n筑基\n金丹\n元婴'} value={(draft.power_ranks ?? []).join('\n')} onChange={(event) => setRanksText(event.target.value)} disabled={busy} />
    </div>
  );

  return (
    <div className="modal-overlay" onMouseDown={() => { if (!busy) onClose(); }}>
      <div className="modal-card project-wizard" role="dialog" aria-modal="true" aria-label="新建作品" onMouseDown={(event) => event.stopPropagation()}>
        <div className="mc-head">
          <div><span className="mc-eyebrow">新建作品 · 第 {step + 1}/3 步</span><h3 className="cn">＋ 开一本新书</h3></div>
          <button type="button" className="modal-close" aria-label="关闭" onClick={onClose} disabled={busy}>✕</button>
        </div>

        <ol className="wizard-steps" aria-label="新建项目步骤">
          {STEP_LABELS.map((label, index) => (
            <li key={label} className={`${index === step ? 'active' : ''}${index < step ? ' complete' : ''}`} aria-current={index === step ? 'step' : undefined}>
              <span>{index + 1}</span>{label}
            </li>
          ))}
        </ol>

        <form className="nf-form single" onSubmit={(event) => void handleSubmit(event)}>
          {step === 0 && (
            <section className="wizard-pane" aria-labelledby="wizard-basics-title">
              <div className="wizard-pane-head"><span>基础信息</span><h4 id="wizard-basics-title">先确定作品语言</h4><p>界面语言与作品语言彼此独立；这里选择创作助手最终写作的语言。</p></div>
              <div className="nf-field full">
                <label htmlFor="cp-name">书名<span className="setting-optional">（可留空，由系统生成）</span></label>
                <input id="cp-name" className="nf-input" type="text" placeholder={isEnglish ? '可留空；例如：The Lost Crown' : '可留空；例如：万古第一剑修'} value={name} onChange={(event) => setName(event.target.value)} autoFocus disabled={busy} />
              </div>
              <div className="nf-field full">
                <label htmlFor="cp-language">作品语言</label>
                <select id="cp-language" className="nf-select" value={contentLanguage} onChange={(event) => handleLanguageChange(event.target.value as ContentLanguage)} disabled={busy}>
                  <option value="zh-CN">{contentLanguageLabel('zh-CN', true)}</option>
                  <option value="en-US">{contentLanguageLabel('en-US', true)}</option>
                </select>
                <small className="field-help">不会跟随浏览器地区自动写入；创建前由你明确确认。</small>
              </div>
            </section>
          )}

          {step === 1 && (
            <section className="wizard-pane" aria-labelledby="wizard-style-title">
              <div className="wizard-pane-head"><span>语言 · 市场 · 风格</span><h4 id="wizard-style-title">选择读者与写作风格</h4><p>目录来自版本化配置，后续设定、规划和正文将共享同一份规则。</p></div>
              {catalogError && <div className="nf-msg err full" role="alert"><span>⚠</span><span>{catalogError}</span><button type="button" className="nf-btn-sm" onClick={() => setCatalogNonce((value) => value + 1)}>重试</button></div>}
              {catalogLoading && <div className="workspace-loading"><span className="nf-spin" /> 正在加载兼容风格…</div>}
              <div className="wizard-style-grid">
                <div className="wizard-style-fields">
                  <div className="nf-field">
                    <label htmlFor="cp-market">目标市场</label>
                    <select id="cp-market" className="nf-select" value={targetMarket} onChange={(event) => handleMarketChange(event.target.value)} disabled={busy || catalogLoading}>
                      {(MARKET_OPTIONS[contentLanguage] ?? MARKET_OPTIONS['en-US']).map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{targetMarketLabel(option.value)}{option.disabled ? '（暂未开放）' : ''}</option>)}
                    </select>
                  </div>
                  <div className="nf-field">
                    <label htmlFor="cp-format">发布形态</label>
                    <select id="cp-format" className="nf-select" value={publicationFormat} onChange={(event) => { setPublicationFormat(event.target.value); setProfileId(''); clearCandidates(primaryGenre); }} disabled={busy || catalogLoading}>
                      <option value="web_serial">{publicationFormatLabel('web_serial')}</option>
                      <option value="novel" disabled>{publicationFormatLabel('novel')}（暂未开放）</option>
                    </select>
                  </div>
                  <div className="nf-field">
                    <label htmlFor="cp-primary-genre">主类型</label>
                    <select id="cp-primary-genre" className="nf-select" value={primaryGenre} onChange={(event) => handleGenreChange(event.target.value)} disabled={busy || catalogLoading || genres.length === 0}>
                      {genres.map((genre) => <option key={genre.id} value={genre.id}>{catalogGenreLabel(genre)}</option>)}
                    </select>
                  </div>
                  <div className="nf-field">
                    <label htmlFor="cp-audience-rating">读者分级</label>
                    <select id="cp-audience-rating" className="nf-select" value={audienceRating} onChange={(event) => setAudienceRating(event.target.value)} disabled={busy}>
                      <option value="general">{audienceRatingLabel('general')}</option>
                      <option value="young_adult">{audienceRatingLabel('young_adult')}</option>
                      <option value="adult">{audienceRatingLabel('adult')}</option>
                    </select>
                  </div>
                  <div className="nf-field full">
                    <label htmlFor="cp-story-tags">子类型 / 标签<span className="setting-optional">（逗号分隔）</span></label>
                    <input
                      id="cp-story-tags"
                      className="nf-input"
                      value={storyTagsText}
                      onChange={(event) => {
                        setStoryTagsText(event.target.value);
                        setStoryTags(parseStoryTags(event.target.value));
                      }}
                      placeholder={isEnglish ? '例如：成长奇幻、游戏化冒险、温馨治愈' : '例如：升级流、群像、轻松'}
                      disabled={busy}
                    />
                  </div>
                </div>

                <aside className="profile-preview" aria-live="polite">
                  <span>已选写作风格</span>
                  <h5>{catalogProfileLabel(selectedProfile)}</h5>
                  <p>{selectedProfile
                    ? writingProfileDescription(selectedProfile.id, selectedProfile.preview || selectedProfile.description)
                    : '选择风格后，这里会显示写作规则和效果说明。'}</p>
                  {selectedProfile && (
                    <dl>
                      <div><dt>叙事视角</dt><dd>{writingRuleLabel(profileRules.pov)}</dd></div>
                      <div><dt>时态</dt><dd>{writingRuleLabel(profileRules.tense)}</dd></div>
                      <div><dt>节奏</dt><dd>{writingRuleLabel(profileRules.pace)}</dd></div>
                      <div><dt>章节长度</dt><dd>{chapterRange?.length ? `${chapterRange[0]}–${chapterRange[1]} ${isEnglish ? '词' : '字'}` : '—'}</dd></div>
                    </dl>
                  )}
                </aside>
              </div>

              {!catalogLoading && profiles.length > 0 && (
                <div className="profile-cards" role="radiogroup" aria-label="主风格预设">
                  {profiles.map((profile) => (
                    <label key={`${profile.id}:${profile.version}`} className={profile.id === profileId ? 'selected' : ''}>
                      <input type="radio" name="writing-profile" value={profile.id} checked={profile.id === profileId} onChange={() => handleProfileChange(profile.id)} disabled={busy} />
                      <span><b>{catalogProfileLabel(profile)}</b><small>{writingProfileDescription(profile.id, profile.description)}</small><em>第 {profile.version} 版</em></span>
                    </label>
                  ))}
                </div>
              )}

              <button type="button" className="nf-btn-sm profile-advanced-toggle" onClick={() => setShowProfileAdvanced((value) => !value)} disabled={busy}>
                {showProfileAdvanced ? '− 收起高级设置' : '＋ 调整叙事视角、时态与节奏'}
              </button>
              {showProfileAdvanced && (
                <div className="profile-advanced-grid">
                  {[
                    ['pov', '叙事人称', ['third_person_limited', 'first_person', 'third_person_omniscient']],
                    ['tense', '时态', ['past', 'present']],
                    ['pace', '节奏', ['fast', 'balanced', 'slow_burn']],
                    ['prose_density', '文笔密度', ['accessible', 'descriptive', 'literary']],
                    ['dialogue_density', '对话密度', ['low', 'medium', 'high']],
                  ].map(([key, label, values]) => (
                    <div className="nf-field" key={key as string}>
                      <label htmlFor={`cp-profile-${key}`}>{label as string}</label>
                      <select id={`cp-profile-${key}`} className="nf-select" value={String(profileOverrides[key as string] ?? '')} onChange={(event) => setOverride(key as string, event.target.value)} disabled={busy}>
                        <option value="">使用预设（{writingRuleLabel(selectedProfile?.prompt_rules[key as string])}）</option>
                        {(values as string[]).map((value) => <option key={value} value={value}>{writingRuleLabel(value)}</option>)}
                      </select>
                    </div>
                  ))}
                </div>
              )}

              <fieldset className="content-warning-fieldset">
                <legend>内容警告<span>（可选）</span></legend>
                {CONTENT_WARNING_OPTIONS.map((option) => (
                  <label key={option} className="nf-check">
                    <input
                      type="checkbox"
                      checked={contentWarnings.includes(option)}
                      onChange={(event) => setContentWarnings((current) => event.target.checked ? [...current, option] : current.filter((item) => item !== option))}
                    />
                    {contentWarningLabel(option)}
                  </label>
                ))}
              </fieldset>
            </section>
          )}

          {step === 2 && (
            <section className="wizard-pane" aria-labelledby="wizard-plot-title">
              <div className="wizard-pane-head"><span>情节设定</span><h4 id="wizard-plot-title">把脑洞变成可选择的开场</h4><p>{isEnglish ? '候选内容将使用英文，并锁定上一步选择的市场与风格。' : '可以自己填写，也可以让创作助手生成三套候选再逐字段调整。'}</p></div>
              <div className="setup-context-summary"><span>{contentLanguageLabel(contentLanguage, true)}</span><span>{targetMarketLabel(targetMarket)}</span><span>{genreLabel(primaryGenre)}</span><span>{catalogProfileLabel(selectedProfile)}</span></div>
              <div className="nf-field full">
                <label>情节设定<span className="setting-optional">（均为选填）</span></label>
                <div className="setup-mode-seg" role="tablist" aria-label="设定模式">
                  <button type="button" role="tab" aria-selected={mode === 'manual'} className={mode === 'manual' ? 'active' : ''} onClick={() => setMode('manual')} disabled={busy}>✍️ 自己填</button>
                  <button type="button" role="tab" aria-selected={mode === 'generate'} className={mode === 'generate' ? 'active' : ''} onClick={() => setMode('generate')} disabled={busy}>✨ 系统生成选项</button>
                </div>
              </div>

              {mode === 'generate' && (
                <>
                  <div className="nf-field full">
                    <label htmlFor="cp-hints">脑洞 / 要求（可选）</label>
                    <textarea id="cp-hints" className="nf-textarea" rows={2} placeholder={isEnglish ? '例如：弱者成长路线，魔法每次使用都要付出代价……' : '例如：废柴流，开局获得吞噬天赋，复仇主线…'} value={hints} onChange={(event) => setHints(event.target.value)} disabled={busy} />
                  </div>
                  {(candidates.length === 0 || selectedIdx === null) && (
                    <button type="button" className="nf-btn blue" onClick={() => void handleGenerate()} disabled={busy}>
                      {generating ? <><span className="nf-spin" /> 生成中…</> : candidates.length > 0 ? '🔄 重新换一批' : '✨ 生成 3 个候选'}
                    </button>
                  )}
                  {genError && <div className="nf-msg err full" role="alert"><span>⚠</span><span>{genError}</span></div>}
                  {degraded && candidates.length > 0 && <div className="nf-msg full"><span>ℹ️</span><span>当前为示例数据；示例占位不会被保存，可切到「自己填」补充。</span></div>}
                  {candidates.length > 0 && selectedIdx === null && <SettingOptionCards candidates={candidates} selectedIdx={selectedIdx} onPick={handlePick} />}
                  {candidates.length > 0 && selectedIdx !== null && (
                    <div className="setting-picked"><span>✓ 已选候选 {selectedIdx + 1}（下方可编辑）</span><button type="button" className="nf-btn-sm" onClick={() => setSelectedIdx(null)} disabled={busy}>重新挑选</button></div>
                  )}
                </>
              )}

              {showEditor && (
                <>
                  {CORE_FIELDS.map(renderField)}
                  {!showAdvanced && <button type="button" className="nf-btn-sm" onClick={() => setShowAdvanced(true)} disabled={busy}>＋ 更多设定（选填）</button>}
                  {showAdvanced && <>{ADVANCED_FIELDS.map(renderField)}{renderRanks()}<button type="button" className="nf-btn-sm" onClick={() => setShowAdvanced(false)} disabled={busy}>− 收起</button></>}
                  {showFieldTools && lockedKeys.size > 0 && (
                    <button type="button" className="nf-btn blue" onClick={() => void handleGenerate()} disabled={busy}>
                      {generating ? <><span className="nf-spin" /> 生成中…</> : `🔄 重新生成未锁定字段（保留 ${lockedKeys.size} 个锁定）`}
                    </button>
                  )}
                </>
              )}
            </section>
          )}

          {error && <div className="nf-msg err full" role="alert"><span>⚠</span><span>{error}</span></div>}
          <div className="nf-actions wizard-actions">
            <div className="nf-actions-main">
              {step > 0 && <button type="button" className="nf-btn ghost" onClick={() => setStep((step - 1) as WizardStep)} disabled={busy}>← 上一步</button>}
              {step < 2 && <button type="button" className="nf-btn pink" onClick={() => setStep((step + 1) as WizardStep)} disabled={busy || (step === 1 && !catalogReady)}>下一步 →</button>}
              {step === 2 && (
                <button type="submit" className="nf-btn pink" disabled={busy || !catalogReady}>
                  {submitting ? <><span className="nf-spin" /> 创建中…</> : trimmedName ? '⚡ 创建作品' : '✨ 零输入开始创作'}
                </button>
              )}
              <button type="button" className="nf-btn ghost" onClick={onClose} disabled={busy}>取消</button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
