import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import type {
  MemoryHealth,
  MemoryLayerHealth,
  MemoryRebuildResult,
  MemorySettings,
  MemorySettingsPatch,
} from '../../api/types';
import { memorySettingLabel, memoryWarningText, userFacingError } from '../../utils/uiText';

type BooleanSettingKey = 'enable_l2' | 'enable_vector' | 'enable_l3' | 'enable_summaries';
type NumberSettingKey =
  | 'l2_every_n_chapters'
  | 'l2_max_scenes_per_chapter'
  | 'vector_top_k'
  | 'summary_window_chapters';

const EDITABLE_KEYS = [
  'enable_l2',
  'enable_vector',
  'enable_l3',
  'enable_summaries',
  'l2_every_n_chapters',
  'l2_max_scenes_per_chapter',
  'vector_top_k',
  'summary_window_chapters',
] as const;

const BOOLEAN_SETTINGS: Array<{
  key: BooleanSettingKey;
  title: string;
  description: string;
}> = [
  {
    key: 'enable_summaries',
    title: '叙事摘要召回',
    description: '写作前注入近期章摘要、卷概要与全书梗概。',
  },
  {
    key: 'enable_l2',
    title: '场景记忆层',
    description: '按章节聚合场景块，为跨章节剧情检索提供语料。',
  },
  {
    key: 'enable_vector',
    title: '场景向量召回',
    description: '从场景记忆中召回语义相近的历史片段。',
  },
  {
    key: 'enable_l3',
    title: '设定事件提案层',
    description: '识别高价值设定变化并进入审核队列，不直接修改已确认设定。',
  },
];

const NUMBER_SETTINGS: Array<{
  key: NumberSettingKey;
  title: string;
  description: string;
  min: number;
  max: number;
}> = [
  {
    key: 'l2_every_n_chapters',
    title: '场景记忆聚合间隔',
    description: '每多少章自动补齐一次场景记忆。',
    min: 1,
    max: 50,
  },
  {
    key: 'l2_max_scenes_per_chapter',
    title: '单章场景上限',
    description: '控制单章切分粒度与索引规模。',
    min: 1,
    max: 20,
  },
  {
    key: 'vector_top_k',
    title: '向量召回条数',
    description: '每次写作最多召回的相似场景记忆数量。',
    min: 1,
    max: 50,
  },
  {
    key: 'summary_window_chapters',
    title: '章摘要窗口',
    description: '写作前注入最近多少章的叙事摘要。',
    min: 1,
    max: 50,
  },
];

const LAYERS: Array<{
  key: 'l0' | 'l1' | 'l2' | 'summaries';
  label: string;
  icon: string;
  description: string;
}> = [
  { key: 'l0', label: '原始正文层', icon: '📄', description: '草稿文件与全文检索索引' },
  { key: 'l1', label: '证据层', icon: '⚛️', description: '正文提案的可追溯审计记录，不等于已确认设定' },
  { key: 'l2', label: '场景记忆层', icon: '🎞️', description: '场景摘要与语义向量' },
  { key: 'summaries', label: '叙事摘要', icon: '🧭', description: '章摘要、卷概要与全书梗概' },
];

const DETAIL_LABELS: Record<string, string> = {
  files_present: '正文文件',
  files_missing: '缺失文件',
  fts_chunks: '全文检索分块',
  fts_chapters: '全文检索章节',
  fts_coverage_pct: '全文检索覆盖',
  anchored: '有证据原子',
  unanchored: '无锚点原子',
  total_rows: '含历史原子',
  superseded: '历史修订原子',
  failed_chapters: '失败章节',
  vectors: '场景向量',
  vector_rows: '向量总行数',
  stale_vectors: '过期向量',
  orphan_vectors: '孤儿向量',
  vector_coverage_pct: '向量覆盖',
};

function errMessage(err: unknown, fallback: string): string {
  return userFacingError(err, fallback);
}

function overrideSettingLabel(key: string): string {
  return BOOLEAN_SETTINGS.find((item) => item.key === key)?.title
    ?? NUMBER_SETTINGS.find((item) => item.key === key)?.title
    ?? memorySettingLabel(key);
}

function warningMessage(warning: string): string {
  return memoryWarningText(warning);
}

function coverageClass(value: number): string {
  if (value >= 100) return 'complete';
  if (value >= 70) return 'partial';
  return 'incomplete';
}

function safeCoverage(value: number): number {
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

function formatDetail(key: string, value: number | string | boolean | null): string {
  if (value === null) return '—';
  if (key.endsWith('_pct') && typeof value === 'number') return `${value.toFixed(1)}%`;
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function clampIntegerSetting(key: NumberSettingKey, value: number): number {
  const limits = NUMBER_SETTINGS.find((item) => item.key === key);
  const integer = Math.round(value);
  if (!limits) return integer;
  return Math.max(limits.min, Math.min(limits.max, integer));
}

function resultNumber(result: MemoryRebuildResult, section: string, field: string): number | null {
  const value = result[section];
  if (!value || typeof value !== 'object') return null;
  const candidate = (value as Record<string, unknown>)[field];
  return typeof candidate === 'number' ? candidate : null;
}

function rebuildSummary(result: MemoryRebuildResult): string {
  const parts: string[] = [];
  const drafts = resultNumber(result, 'l0', 'drafts');
  const atoms = resultNumber(result, 'l1', 'atoms');
  const chapters = resultNumber(result, 'l2', 'processed');
  const vectors = resultNumber(result, 'vectors', 'indexed');
  if (drafts !== null) parts.push(`原始正文层 ${drafts} 章已索引`);
  if (atoms !== null) parts.push(`证据层 ${atoms} 条记录`);
  if (chapters !== null) parts.push(`场景记忆层 ${chapters} 章已聚合`);
  if (vectors !== null) parts.push(`${vectors} 条向量`);
  return parts.length > 0 ? parts.join(' · ') : '记忆重建已完成';
}

function LayerCard({
  layer,
  label,
  icon,
  description,
}: {
  layer?: MemoryLayerHealth;
  label: string;
  icon: string;
  description: string;
}): JSX.Element {
  const coverage = safeCoverage(layer?.coverage_pct ?? 0);
  const tone = coverageClass(coverage);
  return (
    <article className={`memory-layer-card ${tone}`}>
      <div className="memory-layer-head">
        <span className="memory-layer-icon" aria-hidden="true">{icon}</span>
        <div>
          <h4>{label}</h4>
          <p>{description}</p>
        </div>
        <strong>{coverage.toFixed(1)}%</strong>
      </div>
      <div
        className="memory-progress"
        role="progressbar"
        aria-label={`${label}覆盖率`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={coverage}
      >
        <span style={{ width: `${coverage}%` }} />
      </div>
      <div className="memory-layer-stats">
        <span><b>{layer?.completed_chapters ?? 0}</b> 已完成章节</span>
        <span><b>{layer?.chapters ?? 0}</b> 数据章节</span>
        <span><b>{layer?.rows ?? 0}</b> 条记录</span>
      </div>
      {layer && Object.keys(layer.details).length > 0 && (
        <div className="memory-detail-list">
          {Object.entries(layer.details).map(([key, value]) => (
            <span key={key}>
              {DETAIL_LABELS[key] ?? '补充指标'} <b>{formatDetail(key, value)}</b>
            </span>
          ))}
        </div>
      )}
    </article>
  );
}

export default function MemoryPanel({ projectId }: { projectId: string }): JSX.Element {
  const requestSeq = useRef<number>(0);
  const activeProjectId = useRef<string>(projectId);
  activeProjectId.current = projectId;
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [draft, setDraft] = useState<MemorySettings | null>(null);
  const [health, setHealth] = useState<MemoryHealth | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [rebuilding, setRebuilding] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    const requestProjectId = projectId;
    const isCurrent = () => (
      seq === requestSeq.current && requestProjectId === activeProjectId.current
    );
    setLoading(true);
    setError(null);
    const [settingsResult, healthResult] = await Promise.allSettled([
      api.getMemorySettings(requestProjectId),
      api.getMemoryHealth(requestProjectId),
    ]);
    if (!isCurrent()) return;

    const errors: string[] = [];
    if (settingsResult.status === 'fulfilled') {
      setSettings(settingsResult.value);
      setDraft(settingsResult.value);
    } else {
      errors.push(errMessage(settingsResult.reason, '加载记忆设置失败'));
    }
    if (healthResult.status === 'fulfilled') {
      setHealth(healthResult.value);
    } else {
      errors.push(errMessage(healthResult.reason, '加载记忆健康状态失败'));
    }
    setError(errors.length > 0 ? errors.join('；') : null);
    setLoading(false);
  }, [projectId]);

  useEffect(() => {
    setSettings(null);
    setDraft(null);
    setHealth(null);
    setNotice(null);
    setSaving(false);
    setRebuilding(false);
    void load();
    return () => { requestSeq.current += 1; };
  }, [load]);

  const isOverridden = (key: string): boolean => Boolean(draft?.env_overrides.includes(key));
  const dirty = Boolean(settings && draft && EDITABLE_KEYS.some((key) => settings[key] !== draft[key]));

  const updateBoolean = (key: BooleanSettingKey, value: boolean) => {
    setDraft((current) => current ? { ...current, [key]: value } : current);
    setNotice(null);
  };

  const updateNumber = (key: NumberSettingKey, value: number) => {
    if (!Number.isFinite(value)) return;
    const nextValue = clampIntegerSetting(key, value);
    setDraft((current) => current ? { ...current, [key]: nextValue } : current);
    setNotice(null);
  };

  const save = async () => {
    if (!settings || !draft || !dirty) return;
    const seq = ++requestSeq.current;
    const requestProjectId = projectId;
    const isCurrent = () => (
      seq === requestSeq.current && requestProjectId === activeProjectId.current
    );
    const patch: MemorySettingsPatch = {};
    if (draft.enable_l2 !== settings.enable_l2) patch.enable_l2 = draft.enable_l2;
    if (draft.enable_vector !== settings.enable_vector) patch.enable_vector = draft.enable_vector;
    if (draft.enable_l3 !== settings.enable_l3) patch.enable_l3 = draft.enable_l3;
    if (draft.enable_summaries !== settings.enable_summaries) {
      patch.enable_summaries = draft.enable_summaries;
    }
    if (draft.l2_every_n_chapters !== settings.l2_every_n_chapters) {
      patch.l2_every_n_chapters = clampIntegerSetting(
        'l2_every_n_chapters', draft.l2_every_n_chapters,
      );
    }
    if (draft.l2_max_scenes_per_chapter !== settings.l2_max_scenes_per_chapter) {
      patch.l2_max_scenes_per_chapter = clampIntegerSetting(
        'l2_max_scenes_per_chapter', draft.l2_max_scenes_per_chapter,
      );
    }
    if (draft.vector_top_k !== settings.vector_top_k) {
      patch.vector_top_k = clampIntegerSetting('vector_top_k', draft.vector_top_k);
    }
    if (draft.summary_window_chapters !== settings.summary_window_chapters) {
      patch.summary_window_chapters = clampIntegerSetting(
        'summary_window_chapters', draft.summary_window_chapters,
      );
    }

    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await api.updateMemorySettings(requestProjectId, patch);
      if (!isCurrent()) return;
      setSettings(saved);
      setDraft(saved);
      setNotice('项目记忆设置已保存并生效。');
      try {
        const nextHealth = await api.getMemoryHealth(requestProjectId);
        if (!isCurrent()) return;
        setHealth(nextHealth);
      } catch (healthError) {
        if (isCurrent()) {
          setError(errMessage(healthError, '设置已保存，但刷新健康状态失败'));
        }
      }
    } catch (err) {
      if (isCurrent()) setError(errMessage(err, '保存记忆设置失败'));
    } finally {
      if (isCurrent()) setSaving(false);
    }
  };

  const rebuild = async () => {
    const accepted = window.confirm(
      '将从现有正文重新生成全文索引、证据记录、场景记忆与向量。过程可重放，但可能需要一些时间。继续吗？',
    );
    if (!accepted) return;
    const seq = ++requestSeq.current;
    const requestProjectId = projectId;
    const isCurrent = () => (
      seq === requestSeq.current && requestProjectId === activeProjectId.current
    );
    setRebuilding(true);
    setError(null);
    setNotice(null);
    try {
      const response = await api.rebuildMemory(requestProjectId, {
        rebuild_l0: true,
        rebuild_l1: true,
        rebuild_l2: true,
        rebuild_vectors: true,
        force: true,
      });
      if (!isCurrent()) return;
      if (response.result.health) {
        setHealth(response.result.health);
      } else {
        const nextHealth = await api.getMemoryHealth(requestProjectId);
        if (!isCurrent()) return;
        setHealth(nextHealth);
      }
      setNotice(rebuildSummary(response.result));
    } catch (err) {
      if (isCurrent()) setError(errMessage(err, '重建记忆失败'));
    } finally {
      if (isCurrent()) setRebuilding(false);
    }
  };

  return (
    <div className="studio-panel memory-panel">
      <div className="panel-head">
        <div>
          <h3 className="cn">项目记忆</h3>
          <span className="ph-hint">正文档案 → 可核验原子 → 场景召回；叙事摘要独立展示</span>
        </div>
        <button
          type="button"
          className="nf-btn ghost sm"
          onClick={() => void load()}
          disabled={loading || saving || rebuilding || dirty}
          title={dirty ? '请先保存或撤销当前修改' : '重新读取设置与覆盖率'}
        >
          ↻ 刷新状态
        </button>
      </div>

      {loading && !settings && !health && (
        <span className="nf-loading"><span className="nf-spin" /> 加载记忆状态…</span>
      )}
      {error && <div className="nf-msg err"><span>⚠</span><span>{error}</span></div>}
      {notice && <div className="nf-msg ok"><span>✓</span><span>{notice}</span></div>}

      {health && (
        <section className="memory-section" aria-labelledby="memory-health-title">
          <div className="panel-section-head">
            <div>
              <h4 id="memory-health-title">记忆覆盖</h4>
              <span className="ph-hint">当前写到第 {health.latest_chapter} 章</span>
            </div>
          </div>
          <div className="memory-layer-grid">
            {LAYERS.map((item) => (
              <LayerCard
                key={item.key}
                layer={health.layers[item.key]}
                label={item.label}
                icon={item.icon}
                description={item.description}
              />
            ))}
          </div>

          {health.warnings.length > 0 ? (
            <div className="memory-warnings">
              <b>需要处理</b>
              <ul>{health.warnings.map((warning, index) => <li key={`${index}-${warning}`}>{warningMessage(warning)}</li>)}</ul>
            </div>
          ) : (
            <div className="nf-msg ok memory-healthy"><span>✓</span><span>当前没有记忆层告警。</span></div>
          )}
        </section>
      )}

      {draft && (
        <section className="memory-section" aria-labelledby="memory-settings-title">
          <div className="panel-section-head">
            <div>
              <h4 id="memory-settings-title">项目级策略</h4>
              <span className="ph-hint">每个项目独立保存；部分选项可能由管理员锁定</span>
            </div>
          </div>

          {draft.env_overrides.length > 0 && (
            <div className="nf-notice memory-env-notice">
              <b>管理员已锁定：</b>{draft.env_overrides.map(overrideSettingLabel).join('、')}。页面展示当前生效值；如需调整，请修改服务运行配置。
            </div>
          )}

          <div className="memory-toggle-grid">
            {BOOLEAN_SETTINGS.map((item) => {
              const overridden = isOverridden(item.key);
              return (
                <label className={`memory-toggle${overridden ? ' overridden' : ''}`} key={item.key}>
                  <input
                    type="checkbox"
                    checked={draft[item.key]}
                    onChange={(event) => updateBoolean(item.key, event.target.checked)}
                    disabled={loading || saving || rebuilding || overridden}
                  />
                  <span className="memory-toggle-copy">
                    <b>{item.title}</b>
                    <small>{item.description}</small>
                    {overridden && <em>管理员已锁定</em>}
                  </span>
                </label>
              );
            })}
          </div>

          <div className="memory-number-grid">
            {NUMBER_SETTINGS.map((item) => {
              const overridden = isOverridden(item.key);
              return (
                <label className="nf-field" key={item.key}>
                  <span>{item.title}{overridden && ' · 管理员已锁定'}</span>
                  <input
                    className="nf-input"
                    type="number"
                    min={item.min}
                    max={item.max}
                    step={1}
                    value={draft[item.key]}
                    onChange={(event) => updateNumber(item.key, event.target.valueAsNumber)}
                    disabled={loading || saving || rebuilding || overridden}
                  />
                  <small>{item.description}（{item.min}–{item.max}）</small>
                </label>
              );
            })}
          </div>

          <div className="memory-runtime">
            <span>向量服务</span>
            <b>服务商：{draft.embedding_provider ? '已配置' : '未配置'}</b>
            <span>模型：{draft.embedding_model ? '已配置' : '未配置'}</span>
            {(draft.embedding_provider || draft.embedding_model) && (
              <details>
                <summary>查看技术标识</summary>
                <code>服务商：{draft.embedding_provider || '未配置'} · 模型：{draft.embedding_model || '未配置'}</code>
              </details>
            )}
          </div>

          <div className="nf-actions memory-actions">
            <button type="button" className="nf-btn" onClick={() => void save()} disabled={loading || !dirty || saving || rebuilding}>
              {saving ? '保存中…' : '💾 保存设置'}
            </button>
            <button
              type="button"
              className="nf-btn ghost"
              onClick={() => { setDraft(settings); setNotice(null); }}
              disabled={loading || !dirty || saving || rebuilding}
            >
              撤销修改
            </button>
            <span className="memory-action-spacer" />
            <button type="button" className="nf-btn pink" onClick={() => void rebuild()} disabled={loading || saving || rebuilding}>
              {rebuilding ? '重建中…' : '♻ 一键重建全部记忆'}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
