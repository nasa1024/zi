import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../../api/client';
import type {
  AgentChoiceRequest,
  AgentDecisionOption,
  AgentInteractionMode,
  AgentSessionResponse,
  StoryMap,
} from '../../api/types';
import {
  agentStageLabel,
  arcTypeLabel,
  contentUnitLabel,
  governanceModeLabel,
  interactionModeLabel,
  keyResultStatusLabel,
  planReasonLabel,
  sessionStatusLabel,
  systemCopyText,
  userFacingError,
} from '../../utils/uiText';


interface AgentPanelProps {
  projectId: string;
  contentLanguage?: string | null;
  onChanged: () => void;
}


const EMPTY_MAP: StoryMap = { plan: null, arcs: [], volumes: [], chapters: [] };

const MODE_OPTIONS: Array<{
  value: AgentInteractionMode;
  description: string;
}> = [
  {
    value: 'guided',
    description: '每个关键节点都停下来，由你选择一项。',
  },
  {
    value: 'copilot',
    description: '系统给出推荐，你可一键采用，也可自己改选。',
  },
  {
    value: 'autonomous',
    description: '自动选择并生成指定章数，达到上限后保存退出。',
  },
];

const IMPACT_LABELS: Record<string, string> = {
  ending_direction: '终局方向',
  arc_count: '情节线数量',
  milestone_count: '关键节点数量',
  chapter_range: '章节范围',
  chapter_count: '规划章节数',
  key_results: '本卷目标',
  candidate_count: '候选稿数量',
  length: '正文长度',
  score: '综合评分',
  hard_blocks: '必须解决的问题',
  proposal_count: '待确认设定数量',
  uses_future_guardrails: '后续规划约束',
  canon_changed: '已确认设定',
  reversible: '此选择',
  future_plan_changed: '后续计划',
  is_current_winner: '候选状态',
  source: '方案来源',
};

const VALUE_FIELD_LABELS: Record<string, string> = {
  text: '内容',
  title: '标题',
  name: '名称',
  goal: '目标',
  objective: '目标',
  status: '状态',
  chapter: '章节',
  description: '说明',
  summary: '概要',
  evidence: '依据',
};


function errorMessage(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}


function agentCopyText(
  value: unknown,
  fallback: string,
  contentLanguage?: string | null,
): string {
  const localized = systemCopyText(value, fallback);
  if (contentLanguage?.startsWith('en')) return localized;
  return /[A-Za-z]/.test(localized) ? fallback : localized;
}


function displayValue(
  value: unknown,
  key?: string,
  contentLanguage?: string | null,
  depth = 0,
): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') {
    if (key === 'uses_future_guardrails') return value ? '会遵守' : '不会遵守';
    if (key === 'canon_changed') return value ? '会改变' : '不会改变';
    if (key === 'reversible') return value ? '可以撤回' : '不可撤回';
    if (key === 'future_plan_changed') return value ? '会调整' : '不会调整';
    if (key === 'is_current_winner') return value ? '当前推荐稿' : '备选稿';
    return value ? '是' : '否';
  }
  if (key === 'source') return planReasonLabel(value, 'active');
  if (key === 'status') return keyResultStatusLabel(value);
  if (key === 'chapter_range' && Array.isArray(value) && value.length >= 2) {
    return `第 ${displayValue(value[0])}–${displayValue(value[1])} 章`;
  }
  if (typeof value === 'number') {
    if (key === 'candidate_count') return `${value} 稿`;
    if (key === 'proposal_count' || key === 'hard_blocks') return `${value} 项`;
    if (key === 'chapter_count') return `${value} 章`;
    if (key === 'length') return `${value.toLocaleString()} ${contentUnitLabel(contentLanguage)}`;
    return String(value);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '暂无';
    return value.map((item) => displayValue(item, key, contentLanguage, depth + 1)).join('；');
  }
  if (typeof value === 'object') {
    if (depth >= 3) return '详细信息已记录';
    const record = value as Record<string, unknown>;
    const primary = record.text ?? record.title ?? record.name ?? record.goal ?? record.objective;
    if (primary != null) {
      const details = [
        record.status == null ? '' : keyResultStatusLabel(record.status),
        record.evidence == null || record.evidence === ''
          ? ''
          : `依据：${displayValue(record.evidence, 'evidence', contentLanguage, depth + 1)}`,
      ].filter(Boolean);
      return `${displayValue(primary, undefined, contentLanguage, depth + 1)}${details.length ? `（${details.join('；')}）` : ''}`;
    }
    const details = Object.entries(record)
      .filter(([nestedKey, nested]) => (
        nested !== null
        && nested !== undefined
        && nested !== ''
        && nestedKey !== 'id'
        && !nestedKey.endsWith('_id')
      ))
      .slice(0, 6)
      .map(([nestedKey, nested]) => (
        `${VALUE_FIELD_LABELS[nestedKey] ?? '补充信息'}：${displayValue(nested, nestedKey, contentLanguage, depth + 1)}`
      ));
    return details.join('；') || '详细信息已记录';
  }
  return String(value);
}


function optionPreview(option: AgentDecisionOption, contentLanguage?: string | null): string | null {
  const payload = option.payload;
  const plan = payload.plan;
  if (plan && typeof plan === 'object') {
    const ending = (plan as Record<string, unknown>).ending_direction;
    return ending ? `终局：${displayValue(ending)}` : null;
  }
  const chapters = payload.chapters;
  if (Array.isArray(chapters)) {
    const labels = chapters.slice(0, 5).map((chapter) => {
      if (!chapter || typeof chapter !== 'object') return '';
      const row = chapter as Record<string, unknown>;
      return `第${displayValue(row.chapter)}章 ${displayValue(row.title || row.goal)}`;
    }).filter(Boolean);
    return labels.join('　→　');
  }
  const keyResults = payload.key_results;
  if (Array.isArray(keyResults) && keyResults.length > 0) {
    return `本卷目标：${keyResults.map((item) => displayValue(item, 'key_results', contentLanguage)).join('；')}`;
  }
  return null;
}


function DecisionCard({
  option,
  recommended,
  busy,
  contentLanguage,
  onSelect,
}: {
  option: AgentDecisionOption;
  recommended: boolean;
  busy: boolean;
  contentLanguage?: string | null;
  onSelect: () => void;
}): JSX.Element {
  const preview = optionPreview(option, contentLanguage);
  const impact = Object.entries(option.impact || {}).filter(([, value]) => (
    value !== null && value !== undefined && value !== ''
  ));

  return (
    <article className={`agent-option${recommended ? ' recommended' : ''}`}>
      <div className="agent-option-top">
        <span className="agent-option-index">方案 {option.option_index + 1}</span>
        {recommended && <span className="agent-badge">智能推荐</span>}
        {option.score != null && <span className="agent-score">★ {option.score.toFixed(1)}</span>}
      </div>
      <h4>{agentCopyText(option.title, '未命名方案', contentLanguage)}</h4>
      <p>{agentCopyText(option.summary, '方案说明待补充', contentLanguage)}</p>
      {option.rationale && <div className="agent-rationale">为什么：{agentCopyText(option.rationale, '推荐理由待补充', contentLanguage)}</div>}
      {preview && <div className="agent-option-preview">{preview}</div>}
      {impact.length > 0 && (
        <div className="agent-impact-list">
          {impact.map(([key, value]) => (
            <span key={key}>
              <b>{IMPACT_LABELS[key] ?? '补充影响'}</b> {displayValue(value, key, contentLanguage)}
            </span>
          ))}
        </div>
      )}
      <button type="button" className="nf-btn agent-pick" onClick={onSelect} disabled={busy}>
        {busy ? '处理中…' : '选择这个方案'}
      </button>
    </article>
  );
}


function StoryMapPanel({ map }: { map: StoryMap }): JSX.Element {
  const hasMap = Boolean(map.plan || map.arcs.length || map.volumes.length || map.chapters.length);
  return (
    <section className="agent-map">
      <div className="panel-section-head">
        <span className="cn">🧭 未来故事地图</span>
        <span className="ph-hint">计划是作者意图，不会提前写入已确认设定</span>
      </div>

      {!hasMap ? (
        <div className="nf-empty">选择全书路线后，这里会显示情节弧、里程碑和未来章节窗口。</div>
      ) : (
        <>
          {map.plan && (
            <div className="agent-plan-hero">
              <span>全书路线 · 第 {map.plan.version} 版</span>
              <h4>{map.plan.title}</h4>
              {map.plan.phase_summary && <p>{map.plan.phase_summary}</p>}
              {map.plan.ending_direction && (
                <div className="agent-ending">终局方向：{map.plan.ending_direction}</div>
              )}
            </div>
          )}

          {map.volumes.length > 0 && (
            <div className="agent-volume-row">
              {map.volumes.map((volume) => (
                <div className="agent-volume-chip" key={volume.id}>
                  <span>第 {volume.volume_no} 卷</span>
                  <b>{volume.title}</b>
                  <small>
                    {volume.start_chapter ?? '?'}–{volume.end_chapter ?? '?'} 章
                  </small>
                </div>
              ))}
            </div>
          )}

          {map.arcs.length > 0 && (
            <div className="agent-arcs">
              {map.arcs.map((arc) => (
                <article className="agent-arc" key={arc.id}>
                  <div className="agent-arc-head">
                    <span>{arcTypeLabel(arc.arc_type)}</span>
                    <b>{arc.title}</b>
                    <small>优先级 {arc.priority}</small>
                  </div>
                  <p>{arc.summary}</p>
                  {arc.milestones.length > 0 && (
                    <div className="agent-milestones">
                      {arc.milestones.map((milestone) => (
                        <div className={`agent-milestone ${milestone.status}`} key={milestone.id}>
                          <span className="agent-milestone-dot" />
                          <div>
                            <b>{milestone.title}</b>
                            <p>{milestone.summary}</p>
                            <small>
                              计划窗口：第 {milestone.due_start_chapter ?? '?'}–
                              {milestone.due_end_chapter ?? '?'} 章
                              {milestone.not_before_chapter != null && (
                                <> · 不早于第 {milestone.not_before_chapter} 章</>
                              )}
                            </small>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}

          {map.chapters.length > 0 && (
            <div className="agent-chapter-track">
              {map.chapters.map((chapter) => (
                <article className={`agent-chapter ${chapter.status}`} key={chapter.chapter}>
                  <div>
                    <span>第 {chapter.chapter} 章</span>
                    {Boolean(chapter.locked) && <small>已锁定路线</small>}
                  </div>
                  <b>{chapter.title || '待定章节名'}</b>
                  <p>{chapter.goal || '等待选择章节路线'}</p>
                  {chapter.hook_text && <em>钩子：{chapter.hook_text}</em>}
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}


export default function AgentPanel({ projectId, contentLanguage, onChanged }: AgentPanelProps): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSession = searchParams.get('session');
  const [mode, setMode] = useState<AgentInteractionMode>('guided');
  const [governanceMode, setGovernanceMode] = useState<'human_gate' | 'hybrid' | 'auto_promote'>('human_gate');
  const [hints, setHints] = useState('');
  const [planningHorizon, setPlanningHorizon] = useState(5);
  const [autoChapters, setAutoChapters] = useState(1);
  const [session, setSession] = useState<AgentSessionResponse | null>(null);
  const [recent, setRecent] = useState<AgentSessionResponse[]>([]);
  const [storyMap, setStoryMap] = useState<StoryMap>(EMPTY_MAP);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const modeInfo = useMemo(
    () => MODE_OPTIONS.find((item) => item.value === mode) ?? MODE_OPTIONS[0],
    [mode],
  );

  const acceptSession = (next: AgentSessionResponse): void => {
    setSession(next);
    setStoryMap(next.story_map || EMPTY_MAP);
    setRecent((rows) => [next, ...rows.filter((row) => row.session_id !== next.session_id)]);
    const params = new URLSearchParams(searchParams);
    params.set('session', next.session_id);
    setSearchParams(params, { replace: true });
  };

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setSession(null);
    setRecent([]);
    setStoryMap(EMPTY_MAP);

    Promise.all([api.listAgentSessions(projectId), api.storyMap(projectId)])
      .then(([sessions, map]) => {
        if (!alive) return;
        setRecent(sessions);
        setStoryMap(map);
        const recovered = sessions.find((item) => item.session_id === requestedSession)
          ?? sessions.find((item) => item.status === 'awaiting_choice' || item.status === 'running')
          ?? null;
        if (recovered) {
          setSession(recovered);
          setStoryMap(recovered.story_map || map);
          setMode(recovered.interaction_mode);
        }
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err, '创作记录加载失败，请稍后重试。'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => { alive = false; };
  }, [projectId, requestedSession]);

  const start = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.startAgent(projectId, {
        interaction_mode: mode,
        governance_mode: governanceMode,
        hints: hints.trim() || null,
        planning_horizon: planningHorizon,
        auto_chapters: autoChapters,
      });
      acceptSession(next);
      onChanged();
    } catch (err) {
      setError(errorMessage(err, '创作助手启动失败，请稍后重试。'));
    } finally {
      setBusy(false);
    }
  };

  const choose = async (request: AgentChoiceRequest): Promise<void> => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.chooseAgent(projectId, session.session_id, request);
      acceptSession(next);
      onChanged();
    } catch (err) {
      setError(errorMessage(err, '创作助手操作失败，请稍后重试。'));
    } finally {
      setBusy(false);
    }
  };

  const openRecent = async (sessionId: string): Promise<void> => {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    try {
      const recovered = await api.getAgentSession(projectId, sessionId);
      acceptSession(recovered);
      setMode(recovered.interaction_mode);
    } catch (err) {
      setError(errorMessage(err, '恢复创作记录失败，请稍后重试。'));
    } finally {
      setBusy(false);
    }
  };

  const newSession = (): void => {
    setSession(null);
    const params = new URLSearchParams(searchParams);
    params.delete('session');
    setSearchParams(params, { replace: true });
    setError(null);
  };

  const decision = session?.decision ?? null;
  const canReroll = decision && ['story_plan', 'volume_plan', 'chapter_route'].includes(decision.stage);
  const lastDecision = session?.last_decision ?? null;
  const lastOption = lastDecision?.options.find(
    (option) => option.id === lastDecision.selected_option_id,
  ) ?? null;
  const autoSelection = lastDecision?.context.auto_selection;
  const autoReason = autoSelection && typeof autoSelection === 'object'
    ? String((autoSelection as Record<string, unknown>).reason ?? '')
    : '';

  return (
    <div className="studio-panel agent-panel">
      <div className="panel-head agent-panel-head">
        <div>
          <span className="panel-kicker">作者选择驱动</span>
          <h3 className="cn">✨ 选择，而不是填表</h3>
          <p>创作助手先生成可比较的路线，你选择一项，或直接采用智能推荐。</p>
        </div>
        {recent.length > 0 && (
          <label className="agent-session-picker">
            <span>恢复会话</span>
            <select
              className="nf-select"
              value={session?.session_id ?? ''}
              onChange={(event) => void openRecent(event.target.value)}
              disabled={busy}
            >
              <option value="">选择历史创作记录…</option>
              {recent.map((item, index) => (
                <option key={item.session_id} value={item.session_id}>
                  {agentStageLabel(item.stage)} · {sessionStatusLabel(item.status)} · 记录 {recent.length - index}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {loading ? (
        <div className="nf-loading"><span className="nf-spin" /> 正在恢复创作现场…</div>
      ) : (
        <>
          {(!session || session.status === 'finished') && (
            <section className="agent-launch">
              {session?.status === 'finished' && (
                <div className="agent-finished">
                  <span>✓</span>
                  <div>
                    <b>本次创作会话已保存</b>
                    <p>正文、选择、未来计划和当前进度都已保留，可以开始下一次会话。</p>
                  </div>
                </div>
              )}

              <div className="agent-mode-grid">
                {MODE_OPTIONS.map((item) => (
                  <button
                    type="button"
                    key={item.value}
                    className={`agent-mode-card${mode === item.value ? ' active' : ''}`}
                    onClick={() => setMode(item.value)}
                    disabled={busy}
                  >
                    <b>{interactionModeLabel(item.value)}</b>
                    <span>{item.description}</span>
                  </button>
                ))}
              </div>

              <div className="agent-launch-form">
                <label className="nf-field full">
                  <span>一句可选提示</span>
                  <textarea
                    className="nf-textarea"
                    rows={2}
                    value={hints}
                    onChange={(event) => setHints(event.target.value)}
                    placeholder="可以完全留空；例如：节奏快、悬疑感强、不要系统流。"
                    disabled={busy}
                  />
                </label>
                <label className="nf-field">
                  <span>未来路线窗口</span>
                  <select
                    className="nf-select"
                    value={planningHorizon}
                    onChange={(event) => setPlanningHorizon(Number(event.target.value))}
                    disabled={busy}
                  >
                    {[3, 5, 8, 10].map((value) => (
                      <option key={value} value={value}>向后规划 {value} 章</option>
                    ))}
                  </select>
                </label>
                <label className="nf-field">
                  <span>设定确认方式</span>
                  <select
                    className="nf-select"
                    value={governanceMode}
                    onChange={(event) => setGovernanceMode(event.target.value as typeof governanceMode)}
                    disabled={busy}
                  >
                    {(['human_gate', 'hybrid', 'auto_promote'] as const).map((value) => (
                      <option key={value} value={value}>{governanceModeLabel(value)}</option>
                    ))}
                  </select>
                </label>
                {mode === 'autonomous' && (
                  <label className="nf-field">
                    <span>本次自动生成</span>
                    <input
                      className="nf-input"
                      type="number"
                      min={1}
                      max={50}
                      value={autoChapters}
                      onChange={(event) => setAutoChapters(Number(event.target.value))}
                      disabled={busy}
                    />
                  </label>
                )}
              </div>

              <div className="agent-launch-actions">
                <button type="button" className="nf-btn pink" onClick={() => void start()} disabled={busy}>
                  {busy ? <><span className="nf-spin" /> 创作助手运行中…</> : '开始生成选项'}
                </button>
                <span>{modeInfo.description}</span>
              </div>
            </section>
          )}

          {session && session.status !== 'finished' && (
            <section className="agent-workflow">
              <div className="agent-status-strip">
                <span className="agent-live-dot" />
                <b>{agentStageLabel(session.stage)}</b>
                <span>{interactionModeLabel(session.interaction_mode)} · {sessionStatusLabel(session.status)}</span>
                {session.state.current_chapter != null && (
                  <span>第 {session.state.current_chapter} 章</span>
                )}
                <small>创作记录已保留</small>
                <button type="button" className="nf-btn-sm" onClick={newSession} disabled={busy}>
                  新会话
                </button>
              </div>

              {lastOption && (
                <div className={`agent-choice-recap${lastDecision?.status === 'auto_selected' ? ' auto' : ''}`}>
                  <span>{lastDecision?.status === 'auto_selected' ? '✨ 已自动选择' : '✓ 已选择'}</span>
                  <div>
                    <b>{agentCopyText(lastOption.title, '已选方案', contentLanguage)}</b>
                    {autoReason && <p>{agentCopyText(autoReason, '已根据候选评分自动选择。', contentLanguage)}</p>}
                  </div>
                </div>
              )}

              {decision ? (
                <>
                  <div className="agent-question">
                    <span>当前决策</span>
                    <h4>{agentCopyText(decision.question, '请选择下一步', contentLanguage)}</h4>
                    <p>以下方案只改变未来创作意图；已经发生的内容仍按已确认设定管理。</p>
                  </div>
                  <div className="agent-options">
                    {decision.options.map((option) => (
                      <DecisionCard
                        key={option.id}
                        option={option}
                        recommended={option.id === decision.recommended_option_id}
                        busy={busy}
                        contentLanguage={contentLanguage}
                        onSelect={() => void choose({ action: 'select', option_id: option.id })}
                      />
                    ))}
                  </div>
                  <div className="agent-decision-actions">
                    <button
                      type="button"
                      className="nf-btn blue"
                      onClick={() => void choose({ action: 'auto' })}
                      disabled={busy}
                    >
                      ✨ 采用智能推荐
                    </button>
                    {canReroll && (
                      <button
                        type="button"
                        className="nf-btn ghost"
                        onClick={() => void choose({ action: 'reroll', hints: hints.trim() || null })}
                        disabled={busy}
                      >
                        ↻ 换一批方案
                      </button>
                    )}
                  </div>
                </>
              ) : (
                <div className="nf-loading"><span className="nf-spin" /> 创作助手正在推进下一阶段…</div>
              )}
            </section>
          )}

          {error && (
            <div className="nf-msg err" role="alert">
              <span>⚠</span><span>{error}</span>
            </div>
          )}

          <StoryMapPanel map={storyMap} />

          <div className="agent-advanced-note">
            <b>高级模式仍然保留：</b>
            世界设定、审核队列、卷目标与关键结果，以及传统章节流水线仍可在其他标签中直接编辑与检查。
          </div>
        </>
      )}
    </div>
  );
}
