// CreateProjectModal — 新建项目弹窗。
// 「情节设定」支持两种模式：手填（manual）或系统生成候选供挑选（generate）。
// generate：✨生成 N 个候选 → 卡片挑选 → 字段级编辑 → 单字段 reroll → 🔒锁定再生成。
// 手填永远可用，是所有候选都不满意时的兜底。open=false 时返回 null。

import { useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type {
  PlotSetting,
  ProjectCreateRequest,
  ProjectResponse,
} from '../../api/types';
import SettingOptionCards from './SettingOptionCards';

export interface CreateProjectModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (p: ProjectResponse) => void;
}

interface GenreOption {
  value: string;
  label: string;
}

// value 集合必须与后端 GENRE_VALUES（novelforge/app/models.py）保持一致。
const GENRE_OPTIONS: GenreOption[] = [
  { value: 'xuanhuan', label: '玄幻 · xuanhuan' },
  { value: 'xianxia', label: '修仙 · xianxia' },
  { value: 'dushi', label: '都市 · dushi' },
  { value: 'kehuan', label: '科幻 · kehuan' },
  { value: 'lishi', label: '历史 · lishi' },
  { value: 'wuxia', label: '武侠 · wuxia' },
  { value: 'yanqing', label: '言情 · yanqing' },
  { value: 'xuanyi', label: '悬疑 · xuanyi' },
  { value: 'default', label: '通用 · default' },
];

type Mode = 'manual' | 'generate';

// 可编辑文本字段（power_ranks / genre 单独处理）
type TextKey =
  | 'premise'
  | 'world_setting'
  | 'power_system'
  | 'protagonist'
  | 'main_conflict'
  | 'opening_hook'
  | 'target_audience'
  | 'tone';

// text/textarea = 自由输入；combo = 可输入可下拉（input + datalist）；select = 纯下拉。
type FieldKind = 'text' | 'textarea' | 'combo' | 'select';
interface FieldDef {
  key: TextKey;
  label: string;
  kind: FieldKind;
  options?: string[];                        // combo/select 的静态候选
  optionsFor?: (genre: string) => string[];  // combo 的题材相关候选
}

// 题材相关的「力量体系」候选（可下拉，也可自行输入）
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

const MAIN_CONFLICT_OPTIONS = [
  '复仇雪耻', '逆袭崛起', '争霸天下', '守护苍生', '夺宝奇遇', '家族复兴',
  '探寻真相', '宿命对决', '守护挚爱', '求道长生', '门派恩怨', '王朝更替',
];
const TONE_OPTIONS = [
  '热血', '爽快', '轻松诙谐', '硬核严肃', '暗黑', '治愈', '沉郁', '悬疑紧张', '智斗', '燃向',
];
const TARGET_AUDIENCE_OPTIONS = ['男频', '女频', '全龄向', '不限'];

interface RankTemplate {
  label: string;
  ranks: string[];
}
const RANK_TEMPLATES: RankTemplate[] = [
  { label: '经典修仙 · 九境', ranks: ['炼气', '筑基', '金丹', '元婴', '化神', '炼虚', '合体', '大乘', '渡劫'] },
  { label: '斗气大陆', ranks: ['斗之气', '斗者', '斗师', '大斗师', '斗灵', '斗王', '斗皇', '斗宗', '斗尊', '斗圣', '斗帝'] },
  { label: '武魂 · 魂师', ranks: ['魂士', '魂师', '大魂师', '魂尊', '魂宗', '魂王', '魂帝', '魂圣', '封号斗罗'] },
  { label: '通用 · 凡人到仙', ranks: ['凡人', '后天', '先天', '宗师', '大宗师', '半步封王', '封王', '封帝'] },
];

// 核心字段（默认展示）与进阶字段（折叠在「更多设定」后），减少手填模式的字段墙。
const CORE_FIELDS: FieldDef[] = [
  { key: 'premise', label: '一句话设定', kind: 'text' },
  { key: 'power_system', label: '力量体系', kind: 'combo', optionsFor: powerSystemOptions },
  { key: 'protagonist', label: '主角设定', kind: 'textarea' },
  { key: 'main_conflict', label: '主线冲突', kind: 'combo', options: MAIN_CONFLICT_OPTIONS },
];
const ADVANCED_FIELDS: FieldDef[] = [
  { key: 'world_setting', label: '世界观', kind: 'textarea' },
  { key: 'opening_hook', label: '开篇钩子', kind: 'textarea' },
  { key: 'target_audience', label: '目标读者', kind: 'select', options: TARGET_AUDIENCE_OPTIONS },
  { key: 'tone', label: '基调', kind: 'combo', options: TONE_OPTIONS },
];

// degraded（无 key / 解析失败）时后端返回的占位候选以此前缀标记；不可当真存进项目。
const SENTINEL = '（示例）';

function emptyDraft(genre: string): PlotSetting {
  return { genre, power_ranks: [] };
}

// 清掉占位哨兵值（degraded 示例数据），避免被当真落库。
function stripSentinel(s: PlotSetting): PlotSetting {
  const out = { ...s } as Record<string, unknown>;
  for (const k of Object.keys(out)) {
    const v = out[k];
    if (typeof v === 'string' && v.startsWith(SENTINEL)) {
      out[k] = null;
    }
  }
  return out as unknown as PlotSetting;
}

export default function CreateProjectModal({
  open,
  onClose,
  onCreated,
}: CreateProjectModalProps): JSX.Element | null {
  const [name, setName] = useState<string>('');
  const [mode, setMode] = useState<Mode>('generate');
  const [draft, setDraft] = useState<PlotSetting>(() => emptyDraft('xuanhuan'));
  const [showAdvanced, setShowAdvanced] = useState<boolean>(false);
  const [edited, setEdited] = useState<boolean>(false);

  // generate 模式状态
  const [hints, setHints] = useState<string>('');
  const [candidates, setCandidates] = useState<PlotSetting[]>([]);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  const [lockedKeys, setLockedKeys] = useState<Set<string>>(new Set());
  const [generating, setGenerating] = useState<boolean>(false);
  const [rerollKey, setRerollKey] = useState<string | null>(null);
  const [degraded, setDegraded] = useState<boolean>(false);
  const [genError, setGenError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Reset whenever the modal is (re)opened.
  useEffect(() => {
    if (open) {
      setName('');
      setMode('generate');
      setDraft(emptyDraft('xuanhuan'));
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
    }
  }, [open]);

  // Esc to close (only while open and idle).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting && !generating) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, submitting, generating, onClose]);

  if (!open) {
    return null;
  }

  const trimmedName = name.trim();
  const busy = submitting || generating;
  // Zero-text start is intentional: the backend assigns a working title and
  // the Agent immediately offers story-route choices.
  const canSubmit = !busy;
  const showEditor = mode === 'manual' || selectedIdx !== null;
  const showFieldTools = mode === 'generate' && selectedIdx !== null;

  const setField = (key: TextKey, value: string) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setEdited(true);
  };

  const setRanksText = (text: string) => {
    const ranks = text
      .split('\n')
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    setDraft((d) => ({ ...d, power_ranks: ranks }));
    setEdited(true);
  };

  const applyRankTemplate = (label: string) => {
    const t = RANK_TEMPLATES.find((x) => x.label === label);
    if (t) {
      setDraft((d) => ({ ...d, power_ranks: [...t.ranks] }));
      setEdited(true);
    }
  };

  const toggleLock = (key: string) => {
    setLockedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleGenerate = async () => {
    if (generating) return;
    if (
      candidates.length > 0 &&
      edited &&
      !window.confirm('重新生成会替换当前候选，已编辑的内容将丢失。确定吗？')
    ) {
      return;
    }
    setGenerating(true);
    setGenError(null);
    try {
      const res = await api.suggestSetting({
        name: trimmedName || null,
        genre: draft.genre,
        hints: hints.trim() || null,
        n: 3,
        locked: lockedKeys.size > 0 ? draft : null,
        locked_fields: Array.from(lockedKeys),
      });
      setCandidates(res.candidates);
      setDegraded(res.degraded);
      setSelectedIdx(null);
      setEdited(false);
    } catch (err) {
      setGenError(err instanceof ApiError || err instanceof Error ? err.message : '生成失败');
    } finally {
      setGenerating(false);
    }
  };

  const handlePick = (i: number) => {
    if (
      selectedIdx !== null &&
      edited &&
      !window.confirm('切换候选会丢弃当前编辑的内容。确定吗？')
    ) {
      return;
    }
    setSelectedIdx(i);
    const c = candidates[i];
    // degraded 占位哨兵不预填进可编辑草稿，改由 placeholder 引导。
    setDraft(stripSentinel({ ...c, power_ranks: c.power_ranks ?? [] }));
    setShowAdvanced(true);
    setEdited(false);
  };

  const handleReroll = async (key: string) => {
    if (generating || rerollKey) return;
    setRerollKey(key);
    setGenError(null);
    try {
      const avoid: string[] = [];
      const cur = (draft as unknown as Record<string, unknown>)[key];
      if (typeof cur === 'string' && cur) avoid.push(cur);
      const res = await api.suggestSetting({
        genre: draft.genre,
        locked: draft,
        locked_fields: Array.from(lockedKeys),
        field: key,
        avoid,
        n: 1,
      });
      const next = res.candidates[0];
      if (next) {
        const v = (next as unknown as Record<string, unknown>)[key];
        setDraft((d) => ({ ...d, [key]: v as never }));
        setEdited(true);
      }
    } catch (err) {
      setGenError(err instanceof ApiError || err instanceof Error ? err.message : '重生失败');
    } finally {
      setRerollKey(null);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const cleaned = stripSentinel({ ...draft, genre: draft.genre });
    const req: ProjectCreateRequest = {
      name: trimmedName,
      genre: draft.genre,
      power_system: cleaned.power_system ?? null,
      setting: cleaned,
    };
    try {
      const created = await api.createProject(req);
      onCreated(created);
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : '创建项目失败');
      setSubmitting(false);
    }
  };

  const handleOverlayClick = () => {
    if (!busy) onClose();
  };

  const renderField = ({ key, label, kind, options, optionsFor }: FieldDef): JSX.Element => {
    const locked = lockedKeys.has(key);
    const value = (draft[key] as string | null | undefined) ?? '';
    const opts = optionsFor ? optionsFor(draft.genre) : options ?? [];
    const dlId = `dl-${key}`;
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
                title={locked ? '已锁定（重生时保持不变）' : '锁定此字段'}
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
                onClick={() => handleReroll(key)}
                disabled={busy || rerollKey !== null}
              >
                {rerollKey === key ? <span className="nf-spin" /> : '✨'}
              </button>
            </span>
          )}
        </div>

        {kind === 'textarea' && (
          <textarea
            id={`cp-${key}`}
            className="nf-textarea"
            rows={2}
            value={value}
            onChange={(e) => setField(key, e.target.value)}
            disabled={busy}
          />
        )}
        {kind === 'text' && (
          <input
            id={`cp-${key}`}
            className="nf-input"
            type="text"
            value={value}
            onChange={(e) => setField(key, e.target.value)}
            disabled={busy}
          />
        )}
        {kind === 'combo' && (
          <>
            <input
              id={`cp-${key}`}
              className="nf-input"
              type="text"
              list={dlId}
              placeholder="可下拉选择，也可自行输入"
              value={value}
              onChange={(e) => setField(key, e.target.value)}
              disabled={busy}
            />
            <datalist id={dlId}>
              {opts.map((o) => (
                <option key={o} value={o} />
              ))}
            </datalist>
          </>
        )}
        {kind === 'select' && (
          <select
            id={`cp-${key}`}
            className="nf-select"
            value={value}
            onChange={(e) => setField(key, e.target.value)}
            disabled={busy}
          >
            <option value="">（选填，请选择）</option>
            {value && !opts.includes(value) && <option value={value}>{value}</option>}
            {opts.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        )}
      </div>
    );
  };

  const renderRanks = (): JSX.Element => (
    <div className="nf-field full">
      <div className="setting-field-head">
        <label htmlFor="cp-ranks">力量阶梯（每行一阶，由弱到强）</label>
        {showFieldTools && (
          <span className="setting-field-tools">
            <button
              type="button"
              className="nf-btn-sm"
              aria-label="重新生成：力量阶梯"
              title="重新生成阶梯"
              onClick={() => handleReroll('power_ranks')}
              disabled={busy || rerollKey !== null}
            >
              {rerollKey === 'power_ranks' ? <span className="nf-spin" /> : '✨'}
            </button>
          </span>
        )}
      </div>
      <select
        className="nf-select rank-template"
        value=""
        aria-label="套用力量阶梯模板"
        onChange={(e) => {
          applyRankTemplate(e.target.value);
          e.target.selectedIndex = 0; // 用完归位，保持占位提示
        }}
        disabled={busy}
      >
        <option value="">套用模板…（也可在下方手动输入）</option>
        {RANK_TEMPLATES.map((t) => (
          <option key={t.label} value={t.label}>
            {t.label}（{t.ranks.length} 阶）
          </option>
        ))}
      </select>
      <textarea
        id="cp-ranks"
        className="nf-textarea"
        rows={3}
        placeholder={'炼气\n筑基\n金丹\n元婴'}
        value={(draft.power_ranks ?? []).join('\n')}
        onChange={(e) => setRanksText(e.target.value)}
        disabled={busy}
      />
    </div>
  );

  return (
    <div className="modal-overlay" onMouseDown={handleOverlayClick}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label="新建项目"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mc-head">
          <div>
            <span className="mc-eyebrow">NEW PROJECT</span>
            <h3 className="cn">＋ 开一本新书</h3>
          </div>
          <button
            type="button"
            className="modal-close"
            aria-label="关闭"
            onClick={onClose}
            disabled={busy}
          >
            ✕
          </button>
        </div>

        <form className="nf-form single" onSubmit={handleSubmit}>
          <div className="nf-field full">
            <label htmlFor="cp-name">
              书名 / NAME<span className="setting-optional">（可留空，由系统生成）</span>
            </label>
            <input
              id="cp-name"
              className="nf-input"
              type="text"
              placeholder="可留空；例如：万古第一剑修"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              disabled={busy}
            />
          </div>

          <div className="nf-field full">
            <label htmlFor="cp-genre">题材 / GENRE</label>
            <select
              id="cp-genre"
              className="nf-select"
              value={draft.genre}
              onChange={(e) => {
                setDraft((d) => ({ ...d, genre: e.target.value }));
                setEdited(true);
              }}
              disabled={busy}
            >
              {GENRE_OPTIONS.map((g) => (
                <option key={g.value} value={g.value}>
                  {g.label}
                </option>
              ))}
            </select>
          </div>

          {/* 模式切换：手填 / 生成 */}
          <div className="nf-field full">
            <label>情节设定 / PLOT SETTING<span className="setting-optional">（均为选填）</span></label>
            <div className="setup-mode-seg" role="tablist" aria-label="设定模式">
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'manual'}
                className={mode === 'manual' ? 'active' : ''}
                onClick={() => setMode('manual')}
                disabled={busy}
              >
                ✍️ 自己填
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'generate'}
                className={mode === 'generate' ? 'active' : ''}
                onClick={() => setMode('generate')}
                disabled={busy}
              >
                ✨ 系统生成选项
              </button>
            </div>
          </div>

          {/* 生成模式：脑洞 + 生成按钮 + 候选卡片 */}
          {mode === 'generate' && (
            <>
              <div className="nf-field full">
                <label htmlFor="cp-hints">脑洞 / 要求（可选）</label>
                <textarea
                  id="cp-hints"
                  className="nf-textarea"
                  rows={2}
                  placeholder="例如：废柴流，开局获得吞噬天赋，复仇主线…"
                  value={hints}
                  onChange={(e) => setHints(e.target.value)}
                  disabled={busy}
                />
              </div>

              {(candidates.length === 0 || selectedIdx === null) && (
                <div className="nf-field full">
                  <button
                    type="button"
                    className="nf-btn blue"
                    onClick={handleGenerate}
                    disabled={busy}
                  >
                    {generating ? (
                      <>
                        <span className="nf-spin" /> 生成中…
                      </>
                    ) : candidates.length > 0 ? (
                      <>🔄 重新换一批</>
                    ) : (
                      <>✨ 生成 3 个候选</>
                    )}
                  </button>
                </div>
              )}

              {genError && (
                <div className="nf-msg err full" role="alert">
                  <span>⚠</span>
                  <span>{genError}</span>
                </div>
              )}
              {degraded && candidates.length > 0 && (
                <div className="nf-msg full">
                  <span>ℹ️</span>
                  <span>当前为示例数据；可切到「自己填」手动编辑（示例占位不会被保存）。</span>
                </div>
              )}

              {candidates.length > 0 && selectedIdx === null && (
                <div className="nf-field full">
                  <SettingOptionCards
                    candidates={candidates}
                    selectedIdx={selectedIdx}
                    onPick={handlePick}
                  />
                </div>
              )}
              {candidates.length > 0 && selectedIdx !== null && (
                <div className="nf-field full">
                  <div className="setting-picked">
                    <span>✓ 已选候选 {selectedIdx + 1}（下方可编辑）</span>
                    <button
                      type="button"
                      className="nf-btn-sm"
                      onClick={() => setSelectedIdx(null)}
                      disabled={busy}
                    >
                      重新挑选
                    </button>
                  </div>
                </div>
              )}
            </>
          )}

          {/* 字段编辑器：手填模式始终显示；生成模式选中候选后显示 */}
          {showEditor && (
            <>
              {CORE_FIELDS.map(renderField)}

              {!showAdvanced && (
                <div className="nf-field full">
                  <button
                    type="button"
                    className="nf-btn-sm"
                    onClick={() => setShowAdvanced(true)}
                    disabled={busy}
                  >
                    ＋ 更多设定（选填）
                  </button>
                </div>
              )}
              {showAdvanced && (
                <>
                  {ADVANCED_FIELDS.map(renderField)}
                  {renderRanks()}
                  <div className="nf-field full">
                    <button
                      type="button"
                      className="nf-btn-sm"
                      onClick={() => setShowAdvanced(false)}
                      disabled={busy}
                    >
                      − 收起
                    </button>
                  </div>
                </>
              )}

              {/* 锁定后重生其余：与字段级 🔒 控件就近，避免滚回顶部 */}
              {showFieldTools && lockedKeys.size > 0 && (
                <div className="nf-field full">
                  <button
                    type="button"
                    className="nf-btn blue"
                    onClick={handleGenerate}
                    disabled={busy}
                  >
                    {generating ? (
                      <>
                        <span className="nf-spin" /> 生成中…
                      </>
                    ) : (
                      <>🔄 重生未锁定字段（保留 {lockedKeys.size} 个锁定）</>
                    )}
                  </button>
                </div>
              )}
            </>
          )}

          {error && (
            <div className="nf-msg err full" role="alert">
              <span>⚠</span>
              <span>{error}</span>
            </div>
          )}

          <div className="nf-actions">
            <div className="nf-actions-main">
              <button type="submit" className="nf-btn pink" disabled={!canSubmit}>
                {submitting ? (
                  <>
                    <span className="nf-spin" /> 创建中…
                  </>
                ) : (
                  <>{trimmedName ? '⚡ 创建项目' : '✨ 零输入开始创作'}</>
                )}
              </button>
              <button type="button" className="nf-btn ghost" onClick={onClose} disabled={busy}>
                取消
              </button>
            </div>
            {!trimmedName && !busy && (
              <span className="nf-actions-hint">无需填写背景或故事线；创建后直接进入 Agent 选择路线。</span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
