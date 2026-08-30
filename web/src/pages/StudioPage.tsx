import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, api } from '../api/client';
import { useHealth, useProjects } from '../api/hooks';
import type { AgentSessionResponse, ProjectResponse } from '../api/types';
import CreateProjectModal from '../components/studio/CreateProjectModal';
import {
  contentLanguageLabel,
  genreLabel,
  userFacingError,
  writingProfileLabel,
} from '../utils/uiText';
import '../styles/studio.css';
import '../styles/studio-page.css';
import '../styles/workspace.css';
import '../styles/studio-redesign.css';

function projectProfileLabel(project: ProjectResponse): string {
  const identifier = project.writing_profile_id ?? 'cn_web_novel_v1';
  const displayName = (
    project.writing_profile_summary
    && typeof project.writing_profile_summary === 'object'
    && project.writing_profile_summary.display_name
  )
    ? project.writing_profile_summary.display_name
    : project.writing_profile_name;
  const mapped = writingProfileLabel(identifier);
  return mapped === '自定义写作风格'
    ? writingProfileLabel(identifier, displayName)
    : mapped;
}

function projectChapter(project: ProjectResponse, session?: AgentSessionResponse | null): number {
  const current = Number(session?.state.current_chapter);
  return Number.isFinite(current) && current > 0
    ? Math.floor(current)
    : Math.max(1, (project.chapter_count || 0) + 1);
}

type ShelfFilter = 'all' | 'needs_decision' | 'ready_to_write' | 'new';
type ShelfSort = 'recent' | 'progress' | 'title';
type ShelfStatusKey = ShelfFilter | 'working' | 'done' | 'syncing' | 'unknown';

type SessionLookup =
  | { state: 'loading' }
  | { state: 'known'; session: AgentSessionResponse | null }
  | { state: 'unknown'; message: string };

interface ShelfStatus {
  key: ShelfStatusKey;
  label: string;
  detail: string;
  activity: string;
  tone: 'decision' | 'working' | 'ready' | 'new' | 'done' | 'unknown';
  path: string | null;
  cta: string;
}

const STAGE_COPY: Record<string, { label: string; detail: string }> = {
  story_plan: { label: '等待你定方向', detail: '3 条全书路线已经准备好' },
  volume_plan: { label: '等待你排这一卷', detail: '冲突与节奏方案已经准备好' },
  chapter_route: { label: '等待你排未来章节', detail: '下一组章节路线正在等你确认' },
  chapter_action: { label: '可以开始写作', detail: '章节契约已锁定，下一步是生成正文' },
  draft_choice: { label: '等待你选正文', detail: '3 份候选稿已经生成' },
  post_chapter: { label: '本章已完成', detail: '看看偏航，再决定是否继续' },
  bootstrap: { label: '准备创作', detail: '先说一句脑洞就可以开始' },
  finished: { label: '已保存', detail: '随时可以从这里继续' },
};

function relativeDate(value: string | undefined): string {
  if (!value) return '最近活动';
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return '最近活动';
  const diff = Math.max(0, Date.now() - time);
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚创建';
  if (minutes < 60) return `${minutes} 分钟前创建`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前创建`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前创建`;
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(time);
}

function sessionRank(session: AgentSessionResponse | null): number {
  if (!session) return 0;
  if (session.status === 'running') return 5;
  if (session.status === 'awaiting_choice') return 4;
  if (session.stage === 'draft_choice') return 3;
  if (session.stage === 'post_chapter') return 2;
  return 1;
}

function latestSession(sessions: AgentSessionResponse[]): AgentSessionResponse | null {
  return sessions.slice().sort((a, b) => sessionRank(b) - sessionRank(a))[0] ?? null;
}

function spotlightRank(status: ShelfStatus): number {
  if (status.key === 'needs_decision') return 5;
  if (status.key === 'ready_to_write') return 4;
  if (status.key === 'new') return 3;
  if (status.key === 'working') return 2;
  return 1;
}

function sessionErrorMessage(error: unknown): string {
  return userFacingError(error, '暂时无法确认上次创作进度，请稍后重试。');
}

function statusForProject(project: ProjectResponse, lookup: SessionLookup): ShelfStatus {
  const id = encodeURIComponent(project.project_id);

  if (lookup.state === 'loading') {
    return {
      key: 'syncing',
      label: '正在同步停点',
      detail: '正在确认这本书停在哪一步',
      activity: '同步完成后会开放正确的继续入口',
      tone: 'working',
      path: null,
      cta: '正在同步',
    };
  }

  if (lookup.state === 'unknown') {
    return {
      key: 'unknown',
      label: '状态未知',
      detail: '暂时无法确认上次创作进度',
      activity: lookup.message,
      tone: 'unknown',
      path: null,
      cta: '暂不可继续',
    };
  }

  const session = lookup.session;
  const chapter = projectChapter(project, session);

  if (session?.status === 'running') {
    return {
      key: 'working',
      label: '创作助手正在工作',
      detail: '完成后会把下一步放回书架',
      activity: `正在准备第 ${chapter} 章`,
      tone: 'working',
      // AgentPanel is the checkpoint owner for every Agent stage. Keeping the
      // session id on the planning route guarantees that a running session can
      // be recovered instead of opening a writing desk that ignores it.
      path: `/studio/${id}/plan?session=${encodeURIComponent(session.session_id)}`,
      cta: '查看进度',
    };
  }

  if (session?.status === 'awaiting_choice') {
    const copy = STAGE_COPY[session.stage] ?? { label: '等待你的选择', detail: '创作现场已经停在这里' };
    return {
      key: 'needs_decision',
      label: copy.label,
      detail: copy.detail,
      activity: session.stage === 'draft_choice'
        ? `第 ${chapter} 章候选稿已生成`
        : `创作停在「${copy.label}」`,
      tone: 'decision',
      path: `/studio/${id}/plan?session=${encodeURIComponent(session.session_id)}`,
      cta: session.stage === 'draft_choice'
        ? '比较候选'
        : session.stage === 'post_chapter'
          ? '核对本章'
          : '继续选择',
    };
  }

  if (session?.stage === 'post_chapter') {
    return {
      key: 'done',
      label: '本章已完成',
      detail: '对照计划与实际，再决定下一步',
      activity: `第 ${chapter} 章已落盘，等你对账`,
      tone: 'ready',
      path: `/studio/${id}/plan?session=${encodeURIComponent(session.session_id)}`,
      cta: '查看本章',
    };
  }

  if (session?.stage === 'chapter_action') {
    return {
      key: 'ready_to_write',
      label: '可以开始写作',
      detail: '章节契约已锁定，下一步是生成正文',
      activity: `第 ${chapter} 章已经规划完成`,
      tone: 'ready',
      path: `/studio/${id}/plan?session=${encodeURIComponent(session.session_id)}`,
      cta: '进入写作决策',
    };
  }

  if (session?.stage === 'finished' && project.chapter_count > 0) {
    return {
      key: 'ready_to_write',
      label: '可以继续写作',
      detail: '上次创作已保存，下一章在等你',
      activity: `已写完 ${project.chapter_count} 章`,
      tone: 'ready',
      path: `/studio/${id}/write/${chapter}`,
      cta: '继续写作',
    };
  }

  if (!session && project.chapter_count > 0) {
    return {
      key: 'ready_to_write',
      label: '可以继续写作',
      detail: '从上次落笔的地方接着走',
      activity: `已写完 ${project.chapter_count} 章`,
      tone: 'ready',
      path: `/studio/${id}/write/${chapter}`,
      cta: '继续写作',
    };
  }

  return {
    key: 'new',
    label: '刚刚开场',
    detail: '先选一条故事方向，创作助手会继续搭建路线',
    activity: '还没有生成章节',
    tone: 'new',
    path: `/studio/${id}/plan`,
    cta: '开始定方向',
  };
}

function StudioNav(): JSX.Element {
  const { online, version } = useHealth();
  const label = online === null ? '连接中…' : online ? (version ? `v${version}` : '在线') : '离线';
  return (
    <nav className="studio-nav">
      <div className="wrap sn-inner">
        <Link to="/" className="sn-back"><span className="sn-back-arrow">←</span><span>主页</span></Link>
        <div className="sn-brand"><span className="spark">✦</span>zi<span className="sn-label">创作台</span></div>
        <div className="sn-health"><span className={`picker-health-dot${online ? ' online' : ''}`} />{label}</div>
      </div>
    </nav>
  );
}

export default function StudioPage(): JSX.Element {
  const navigate = useNavigate();
  const [modalOpen, setModalOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<ShelfFilter>('all');
  const [sort, setSort] = useState<ShelfSort>('recent');
  const [sessionByProject, setSessionByProject] = useState<Record<string, SessionLookup>>({});
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionRefreshKey, setSessionRefreshKey] = useState(0);
  const { projects, loading, error, refetch, addProject } = useProjects();

  useEffect(() => {
    let alive = true;
    if (projects.length === 0) {
      setSessionByProject({});
      setSessionsLoading(false);
      return () => { alive = false; };
    }

    setSessionByProject(Object.fromEntries(
      projects.map((project) => [project.project_id, { state: 'loading' } satisfies SessionLookup]),
    ));
    setSessionsLoading(true);
    void Promise.all(projects.map(async (project) => {
      try {
        const sessions = await api.listAgentSessions(project.project_id);
        return [project.project_id, {
          state: 'known',
          session: latestSession(sessions),
        } satisfies SessionLookup] as const;
      } catch (sessionError) {
        if (
          sessionError instanceof ApiError
          && [404, 405, 501].includes(sessionError.status)
        ) {
          // Compatibility fallback for deployments that do not expose Agent
          // sessions. All other failures stay closed so a stale route is never
          // presented as safe to resume.
          return [project.project_id, {
            state: 'known',
            session: null,
          } satisfies SessionLookup] as const;
        }
        return [project.project_id, {
          state: 'unknown',
          message: sessionErrorMessage(sessionError),
        } satisfies SessionLookup] as const;
      }
    })).then((entries) => {
      if (!alive) return;
      setSessionByProject(Object.fromEntries(entries));
    }).finally(() => {
      if (alive) setSessionsLoading(false);
    });
    return () => { alive = false; };
  }, [projects, sessionRefreshKey]);

  const handleCreated = useCallback((project: ProjectResponse) => {
    addProject(project);
    setModalOpen(false);
    refetch();
    navigate(`/studio/${encodeURIComponent(project.project_id)}/plan`);
  }, [addProject, navigate, refetch]);

  const cards = useMemo(() => projects.map((project, index) => {
    const lookup = sessionByProject[project.project_id] ?? { state: 'loading' };
    return {
      project,
      status: statusForProject(project, lookup),
      index,
    };
  }), [projects, sessionByProject]);

  const visibleCards = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    const filtered = cards.filter(({ project, status }) => {
      const haystack = [
        project.name,
        project.premise,
        project.genre,
        project.primary_genre,
        ...(project.story_tags ?? []),
      ].filter(Boolean).join(' ').toLocaleLowerCase();
      const matchesSearch = !query || haystack.includes(query);
      const matchesFilter = filter === 'all'
        || (filter === 'needs_decision' && status.key === 'needs_decision')
        || (filter === 'ready_to_write' && status.key === 'ready_to_write')
        || (filter === 'new' && status.key === 'new');
      return matchesSearch && matchesFilter;
    });
    return filtered.sort((a, b) => {
      if (sort === 'title') return a.project.name.localeCompare(b.project.name, 'zh-CN');
      if (sort === 'progress') return (b.project.chapter_count || 0) - (a.project.chapter_count || 0);
      const byDate = Date.parse(b.project.created_at) - Date.parse(a.project.created_at);
      return Number.isFinite(byDate) && byDate !== 0 ? byDate : b.index - a.index;
    });
  }, [cards, filter, search, sort]);

  const spotlight = visibleCards.reduce<(typeof visibleCards)[number] | null>((best, item) => {
    if (!best || spotlightRank(item.status) > spotlightRank(best.status)) return item;
    return best;
  }, null);
  const decisionCount = cards.filter(({ status }) => status.key === 'needs_decision').length;
  const chapterCount = cards.reduce((sum, item) => sum + (item.project.chapter_count || 0), 0);
  const unknownSessionCount = cards.filter(({ status }) => status.key === 'unknown').length;

  return (
    <>
      <StudioNav />
      <main className="studio-picker-page shelf-redesign">
        <header className="shelf-hero">
          <div className="shelf-hero-copy">
            <span className="shelf-eyebrow">你的创作书桌 · {projects.length} 本作品</span>
            <h1>今天，从最顺手的下一步开始。</h1>
            <p>每一本书都记得你停在哪里。选一本，zi 会把下一步放到你面前。</p>
          </div>
          <div className="shelf-hero-actions">
            <button type="button" className="picker-new-book" onClick={() => setModalOpen(true)}>＋ 新建一本书</button>
            <div className="shelf-hero-stats" aria-label="书架概览">
              <span><b>{chapterCount}</b> 已写章节</span>
              <span><b>{decisionCount}</b> 待你决定</span>
            </div>
          </div>
        </header>

        {loading ? (
          <div className="workspace-loading"><span className="nf-spin" /> 正在清点书架…</div>
        ) : error ? (
          <section className="studio-picker-empty shelf-load-error" role="alert">
            <span>书架暂不可用</span>
            <h2>书架暂时没能打开</h2>
            <p>{userFacingError(new Error(error), '书架加载失败，请稍后重试。')}</p>
            <button type="button" onClick={refetch}>重试加载</button>
          </section>
        ) : projects.length === 0 ? (
          <section className="studio-picker-empty shelf-empty">
            <span>书架空空</span>
            <h2>说一句脑洞，先把第一幕点亮</h2>
            <p>不用先填完整设定。告诉 zi 你想写什么，它会先给你 3 条可比较的故事方向。</p>
            <button type="button" onClick={() => setModalOpen(true)}>开始第一本书</button>
          </section>
        ) : (
          <>
            {spotlight && (
              <section className="shelf-next-card" aria-labelledby="shelf-next-title">
                <div className="shelf-next-marker" aria-hidden="true">下一步</div>
                <div className="shelf-next-copy">
                  <span className="shelf-section-label">推荐继续</span>
                  <h2 id="shelf-next-title">{spotlight.project.name}</h2>
                  <p>{spotlight.status.detail}</p>
                  <div className="shelf-next-meta">
                    <span className={`shelf-status-pill ${spotlight.status.tone}`}>{spotlight.status.label}</span>
                    <span>{spotlight.status.activity}</span>
                  </div>
                </div>
                <button
                  type="button"
                  className="shelf-continue-cta"
                  aria-label={`${spotlight.status.cta}：${spotlight.project.name}`}
                  disabled={!spotlight.status.path}
                  onClick={() => {
                    if (spotlight.status.path) navigate(spotlight.status.path);
                  }}
                >
                  {spotlight.status.cta}
                  {spotlight.status.path && <span aria-hidden="true">→</span>}
                </button>
              </section>
            )}

            <section className="shelf-toolbar" aria-label="筛选和排序作品">
              <label className="shelf-search">
                <span>搜索作品</span>
                <input
                  type="search"
                  aria-label="搜索作品"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="书名、类型或脑洞…"
                />
              </label>
              <label className="shelf-filter">
                <span>查看</span>
                <select aria-label="筛选作品状态" value={filter} onChange={(event) => setFilter(event.target.value as ShelfFilter)}>
                  <option value="all">全部作品</option>
                  <option value="needs_decision">等我做决定</option>
                  <option value="ready_to_write">可以写作</option>
                  <option value="new">刚刚开始</option>
                </select>
              </label>
              <div className="shelf-sort" role="group" aria-label="作品排序">
                <span>排序</span>
                {([
                  ['recent', '最近'],
                  ['progress', '进度'],
                  ['title', '标题'],
                ] as const).map(([value, label]) => (
                  <button
                    type="button"
                    key={value}
                    className={sort === value ? 'active' : ''}
                    aria-pressed={sort === value}
                    onClick={() => setSort(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </section>

            {sessionsLoading && <div className="shelf-sync-note" role="status"><span className="nf-spin" /> 正在同步每本书的下一步…</div>}
            {!sessionsLoading && unknownSessionCount > 0 && (
              <div className="shelf-session-warning" role="alert">
                <span>{unknownSessionCount} 本书的创作进度暂时无法确认，继续入口已安全锁定。</span>
                <button type="button" onClick={() => setSessionRefreshKey((value) => value + 1)}>重试同步状态</button>
              </div>
            )}

            {visibleCards.length === 0 ? (
              <section className="shelf-no-results" role="status">
                <span>没有匹配</span>
                <h2>没有符合条件的作品</h2>
                <p>换一个关键词或清除筛选，书架里的故事还在等你。</p>
                <button type="button" onClick={() => { setSearch(''); setFilter('all'); }}>清除筛选</button>
              </section>
            ) : (
              <section className="shelf-projects" aria-label="我的作品">
                <div className="shelf-section-heading">
                  <div><span className="shelf-section-label">我的作品</span><h2>{visibleCards.length} 本正在生长</h2></div>
                  <span className="shelf-section-help">状态会随着每次选择自动更新</span>
                </div>
                <div className="project-bookshelf">
                  {visibleCards.map(({ project, status, index }) => (
                    <article className="project-book shelf-project-card" key={project.project_id}>
                      <div className="project-book-index">作品 / {String(index + 1).padStart(2, '0')}</div>
                      <div className="shelf-card-topline">
                        <span className="project-book-genre">{genreLabel(project.primary_genre || project.genre)}</span>
                        <span className={`shelf-status-pill ${status.tone}`}>{status.label}</span>
                      </div>
                      <h2>{project.name}</h2>
                      <p>{project.premise || '这本书还在等待一句清晰的核心命题。'}</p>
                      <div className="project-book-profile">
                        <span>{contentLanguageLabel(project.content_language, true)}</span>
                        <span>{projectProfileLabel(project)}</span>
                      </div>
                      <div className="shelf-next-step">
                        <span>下一步</span>
                        <b>{status.detail}</b>
                        <small>{status.activity} · {relativeDate(project.created_at)}</small>
                      </div>
                      <dl>
                        <div><dt>章节</dt><dd>{project.chapter_count}</dd></div>
                        <div><dt>已确认设定</dt><dd>{project.canon_fact_count}</dd></div>
                      </dl>
                      <div className="project-book-actions">
                        <button type="button" onClick={() => navigate(`/studio/${encodeURIComponent(project.project_id)}/plan`)}>看规划</button>
                        <button
                          type="button"
                          className="primary"
                          aria-label={`${status.cta}：${project.name}`}
                          disabled={!status.path}
                          onClick={() => {
                            if (status.path) navigate(status.path);
                          }}
                        >
                          {status.cta}{status.path ? ' →' : ''}
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </main>
      <CreateProjectModal open={modalOpen} onClose={() => setModalOpen(false)} onCreated={handleCreated} />
    </>
  );
}
