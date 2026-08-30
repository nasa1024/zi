import { Link, NavLink, Outlet, useLocation, useOutletContext, useParams } from 'react-router-dom';
import { useHealth, useProject } from '../../api/hooks';
import type { ProjectResponse } from '../../api/types';
import {
  contentLanguageLabel,
  userFacingError,
  writingProfileLabel,
} from '../../utils/uiText';
import StoryFlowRail, { type StoryFlowStepKey } from './StoryFlowRail';

export interface WorkspaceOutletContext {
  project: ProjectResponse;
  refetchProjects: () => void;
}

export function useWorkspaceOutlet(): WorkspaceOutletContext {
  return useOutletContext<WorkspaceOutletContext>();
}

const WORKSPACE_LINKS = [
  { suffix: 'write', label: '写作', short: '写' },
  { suffix: 'plan', label: '规划', short: '规' },
  { suffix: 'world', label: '世界', short: '世' },
  { suffix: 'review', label: '审核', short: '审' },
  { suffix: 'settings', label: '设置', short: '设' },
];

function projectProfileLabel(project: ProjectResponse): string {
  const summary = project.writing_profile_summary && typeof project.writing_profile_summary === 'object'
    ? project.writing_profile_summary
    : null;
  const displayName = summary?.display_name ?? project.writing_profile_name;
  const localizedDisplayName = displayName && /[\u3400-\u9fff]/u.test(displayName)
    ? displayName
    : null;
  return writingProfileLabel(project.writing_profile_id ?? summary?.id, localizedDisplayName);
}

export default function WorkspaceShell(): JSX.Element {
  const { projectId = '', chapter: chapterParam } = useParams();
  const location = useLocation();
  const { project, loading, error, refetch } = useProject(projectId);
  const { online } = useHealth();

  if (loading) {
    return (
      <main className="workspace-route-state" aria-live="polite">
        <span className="nf-spin" /> 正在装订创作现场…
      </main>
    );
  }

  if (error || !project) {
    return (
      <main className="workspace-route-state">
        <span className="workspace-route-mark">未找到</span>
        <h1>{error ? '工作区暂时无法打开' : '这本书不在书架上'}</h1>
        <p>{error ? userFacingError(new Error(error), '加载这本书失败，请稍后重试。') : '没有找到这本书，请返回书架重新选择。'}</p>
        <Link className="nf-btn" to="/studio">返回项目书架</Link>
      </main>
    );
  }

  const chapterFromPath = Number.parseInt(
    chapterParam ?? location.pathname.match(/\/write\/(\d+)/)?.[1] ?? '',
    10,
  );
  const currentChapter = Number.isFinite(chapterFromPath) && chapterFromPath > 0
    ? chapterFromPath
    : location.pathname.endsWith('/plan')
      ? Math.max(1, project.chapter_count + 1)
      : Math.max(1, project.chapter_count);
  const sessionId = new URLSearchParams(location.search).get('session');
  const workspaceSearch = sessionId ? `?session=${encodeURIComponent(sessionId)}` : '';
  let currentStep: StoryFlowStepKey | null = null;
  if (location.pathname.includes('/write/')) {
    currentStep = location.hash.includes('candidates')
      ? 'select'
      : location.hash.includes('proofread')
        ? 'proofread'
        : location.hash.includes('continue')
          ? 'continue'
          : 'write';
  } else if (location.pathname.endsWith('/plan')) {
    currentStep = location.hash.includes('future') ? 'future' : 'direction';
  }

  return (
    <div className="workspace-shell">
      <header className="workspace-topbar">
        <Link to="/" className="workspace-brand" aria-label="zi 主页">
          <span className="workspace-spark" aria-hidden="true">✦</span>
          <span>zi</span>
          <small>创作台</small>
        </Link>

        <Link className="workspace-project-switcher" to="/studio" aria-label="返回书架切换项目">
          <span className="workspace-project-name">{project.name}</span>
          <span className="workspace-project-meta">
            {contentLanguageLabel(project.content_language)} · {projectProfileLabel(project)} · 书架 ↗
          </span>
        </Link>

        <nav className="workspace-primary-nav" aria-label="工作区主导航">
          {WORKSPACE_LINKS.map((item) => (
            <NavLink
              key={item.suffix}
              to={`/studio/${encodeURIComponent(project.project_id)}/${item.suffix === 'write' ? `write/${currentChapter}` : item.suffix}${workspaceSearch}`}
              className={({ isActive }) => {
                const writingActive = item.suffix === 'write' && location.pathname.includes('/write/');
                return `workspace-nav-link${isActive || writingActive ? ' active' : ''}`;
              }}
            >
              <span aria-hidden="true">{item.short}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className={`workspace-health${online ? ' online' : online === false ? ' offline' : ''}`}>
          <span className="workspace-health-dot" aria-hidden="true" />
          <span>{online === null ? '连接中' : online ? '引擎在线' : '引擎离线'}</span>
        </div>
      </header>

      <StoryFlowRail
        projectId={project.project_id}
        currentChapter={currentChapter}
        currentStep={currentStep}
        workspaceSearch={workspaceSearch}
      />

      <Outlet context={{ project, refetchProjects: refetch } satisfies WorkspaceOutletContext} />
    </div>
  );
}
