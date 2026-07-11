import { useEffect, useMemo, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type {
  AgentChoiceRequest,
  AgentDecisionOption,
  AgentInteractionMode,
  AgentSessionResponse,
  StoryMap,
} from '../../api/types';


interface AgentPanelProps {
  projectId: string;
  onChanged: () => void;
}


const EMPTY_MAP: StoryMap = { plan: null, arcs: [], volumes: [], chapters: [] };
const SESSION_STORAGE_PREFIX = 'nf_agent_session:';

const MODE_OPTIONS: Array<{
  value: AgentInteractionMode;
  label: string;
  description: string;
}> = [
  {
    value: 'guided',
    label: '逐步选择',
    description: '每个关键节点都停下来，由你选择一项。',
  },
  {
    value: 'copilot',
    label: '协作模式',
    description: '模型给出推荐，你可一键让 AI 决定，也可自己改选。',
  },
  {
    value: 'autonomous',
    label: '模型决定',
    description: '自动选择并生成指定章数，达到上限后保存退出。',
  },
];

const STAGE_LABELS: Record<string, string> = {
  bootstrap: '准备创作',
  story_plan: '选择全书路线',
  volume_plan: '选择下一卷',
  chapter_route: '选择未来章节路线',
  chapter_action: '确认章节动作',
  draft_choice: '选择正文候选稿',
  post_chapter: '本章完成',
  finished: '会话已保存',
};

const IMPACT_LABELS: Record<string, string> = {
  ending_direction: '终局方向',
  arc_count: '情节弧',
  milestone_count: '里程碑',
  chapter_range: '章节范围',
  chapter_count: '规划章数',
  key_results: '关键结果',
  candidate_count: '候选稿',
  length: '字数',
  score: '评分',
  hard_blocks: '硬冲突',
  proposal_count: '设定提案',
};


function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}


function storedSession(projectId: string): string | null {
  try { return localStorage.getItem(SESSION_STORAGE_PREFIX + projectId); } catch { return null; }
}


function rememberSession(projectId: string, sessionId: string | null): void {
  try {
    const key = SESSION_STORAGE_PREFIX + projectId;
    if (sessionId) localStorage.setItem(key, sessionId);
    else localStorage.removeItem(key);
  } catch { /* localStorage may be unavailable */ }
}


function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(displayValue).join(' · ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}


function optionPreview(option: AgentDecisionOption): string | null {
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
    return `关键结果：${keyResults.map(displayValue).join('；')}`;
  }
  return null;
}


function DecisionCard({
  option,
  recommended,
  busy,
  onSelect,
}: {
  option: AgentDecisionOption;
  recommended: boolean;
  busy: boolean;
  onSelect: () => void;
}): JSX.Element {
  const preview = optionPreview(option);
  const impact = Object.entries(option.impact || {}).filter(([, value]) => (
    value !== null && value !== undefined && value !== false
  ));

  return (
    <article className={`agent-option${recommended ? ' recommended' : ''}`}>
      <div className="agent-option-top">
        <span className="agent-option-index">方案 {option.option_index + 1}</span>
        {recommended && <span className="agent-badge">AI 推荐</span>}
        {option.score != null && <span className="agent-score">★ {option.score.toFixed(1)}</span>}
      </div>
      <h4>{option.title}</h4>
      <p>{option.summary}</p>
      {option.rationale && <div className="agent-rationale">为什么：{option.rationale}</div>}
      {preview && <div className="agent-option-preview">{preview}</div>}
      {impact.length > 0 && (
        <div className="agent-impact-list">
          {impact.slice(0, 6).map(([key, value]) => (
            <span key={key}>
              <b>{IMPACT_LABELS[key] ?? key}</b> {displayValue(value)}
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
        <span className="ph-hint">计划是作者意图，不会提前写入 canon</span>
      </div>

      {!hasMap ? (
        <div className="nf-empty">选择全书路线后，这里会显示情节弧、里程碑和未来章节窗口。</div>
      ) : (
        <>
          {map.plan && (
            <div className="agent-plan-hero">
              <span>全书路线 · v{map.plan.version}</span>
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
                    <span>{arc.arc_type}</span>
                    <b>{arc.title}</b>
                    <small>P{arc.priority}</small>
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


export default function AgentPanel({ projectId, onChanged }: AgentPanelProps): JSX.Element {
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
    rememberSession(projectId, next.session_id);
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
        const remembered = storedSession(projectId);
        const recovered = sessions.find((item) => item.session_id === remembered)
          ?? sessions.find((item) => item.status === 'awaiting_choice' || item.status === 'running')
          ?? null;
        if (recovered) {
          setSession(recovered);
          setStoryMap(recovered.story_map || map);
          setMode(recovered.interaction_mode);
        }
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err, 'Agent 会话加载失败'));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => { alive = false; };
  }, [projectId]);

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
      setError(errorMessage(err, 'Agent 启动失败'));
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
      setError(errorMessage(err, 'Agent 操作失败'));
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
      setError(errorMessage(err, '恢复 Agent 会话失败'));
    } finally {
      setBusy(false);
    }
  };

  const newSession = (): void => {
    setSession(null);
    rememberSession(projectId, null);
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
          <span className="panel-kicker">CHOICE-DRIVEN AUTHOR AGENT</span>
          <h3 className="cn">✨ 选择，而不是填表</h3>
          <p>Agent 先生成可比较的路线，你选择一项，或直接让模型替你决定。</p>
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
              <option value="">选择历史 checkpoint…</option>
              {recent.map((item) => (
                <option key={item.session_id} value={item.session_id}>
                  {STAGE_LABELS[item.stage] ?? item.stage} · {item.status} · {item.session_id.slice(-6)}
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
                    <p>正文、选择、未来计划和 checkpoint 都已保留，可以开始下一次会话。</p>
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
                    <b>{item.label}</b>
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
                  <span>Canon 治理</span>
                  <select
                    className="nf-select"
                    value={governanceMode}
                    onChange={(event) => setGovernanceMode(event.target.value as typeof governanceMode)}
                    disabled={busy}
                  >
                    <option value="human_gate">human_gate · 人审</option>
                    <option value="hybrid">hybrid · 混合</option>
                    <option value="auto_promote">auto_promote · 自动晋升</option>
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
                  {busy ? <><span className="nf-spin" /> Agent 运行中…</> : '开始生成选项'}
                </button>
                <span>{modeInfo.description}</span>
              </div>
            </section>
          )}

          {session && session.status !== 'finished' && (
            <section className="agent-workflow">
              <div className="agent-status-strip">
                <span className="agent-live-dot" />
                <b>{STAGE_LABELS[session.stage] ?? session.stage}</b>
                <span>{session.interaction_mode}</span>
                {session.state.current_chapter != null && (
                  <span>第 {session.state.current_chapter} 章</span>
                )}
                <small>checkpoint · {session.session_id.slice(-8)}</small>
                <button type="button" className="nf-btn-sm" onClick={newSession} disabled={busy}>
                  新会话
                </button>
              </div>

              {lastOption && (
                <div className={`agent-choice-recap${lastDecision?.status === 'auto_selected' ? ' auto' : ''}`}>
                  <span>{lastDecision?.status === 'auto_selected' ? '✨ AI 已选择' : '✓ 已选择'}</span>
                  <div>
                    <b>{lastOption.title}</b>
                    {autoReason && <p>{autoReason}</p>}
                  </div>
                </div>
              )}

              {decision ? (
                <>
                  <div className="agent-question">
                    <span>当前决策</span>
                    <h4>{decision.question}</h4>
                    <p>以下方案只改变未来创作意图；已经发生的故事事实仍由 canon 治理。</p>
                  </div>
                  <div className="agent-options">
                    {decision.options.map((option) => (
                      <DecisionCard
                        key={option.id}
                        option={option}
                        recommended={option.id === decision.recommended_option_id}
                        busy={busy}
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
                      ✨ 让 AI 决定
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
                <div className="nf-loading"><span className="nf-spin" /> Agent 正在推进下一阶段…</div>
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
            世界圣经、审核队列、卷 OKR 和传统章节流水线仍可在其他标签中直接编辑与检查。
          </div>
        </>
      )}
    </div>
  );
}
