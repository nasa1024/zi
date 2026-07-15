import { Link, NavLink, Outlet, useLocation, useOutletContext, useParams } from 'react-router-dom';
import { useHealth, useProject } from '../../api/hooks';
import type { ProjectResponse } from '../../api/types';

export interface WorkspaceOutletContext {
  project: ProjectResponse;
  refetchProjects: () => void;
}

export function useWorkspaceOutlet(): WorkspaceOutletContext {
  return useOutletContext<WorkspaceOutletContext>();
}

const WORKSPACE_LINKS = [
  { suffix: 'write/1', label: '写作', short: 'WR' },
  { suffix: 'plan', label: '规划', short: 'PL' },
  { suffix: 'world', label: '世界', short: 'WD' },
  { suffix: 'review', label: '审核', short: 'RV' },
  { suffix: 'settings', label: '设置', short: 'ST' },
];

export default function WorkspaceShell(): JSX.Element {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const { project, loading, error, refetch } = useProject(projectId);
  const { online, version } = useHealth();

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
        <span className="workspace-route-mark">404</span>
        <h1>{error ? '工作区暂时无法打开' : '这本书不在书架上'}</h1>
        <p>{error ?? `未找到项目 ${projectId}`}</p>
        <Link className="nf-btn" to="/studio">返回项目书架</Link>
      </main>
    );
  }

  return (
    <div className="workspace-shell">
      <header className="workspace-topbar">
        <Link to="/" className="workspace-brand" aria-label="NovelForge 主页">
          <span className="workspace-spark" aria-hidden="true">✦</span>
          <span>NovelForge</span>
          <small>WRITING DESK</small>
        </Link>

        <Link className="workspace-project-switcher" to="/studio" aria-label="返回书架切换项目">
          <span className="workspace-project-name">{project.name}</span>
          <span className="workspace-project-meta">{project.genre} · 书架 ↗</span>
        </Link>

        <nav className="workspace-primary-nav" aria-label="工作区主导航">
          {WORKSPACE_LINKS.map((item) => (
            <NavLink
              key={item.suffix}
              to={`/studio/${encodeURIComponent(project.project_id)}/${item.suffix}`}
              className={({ isActive }) => {
                const writingActive = item.short === 'WR' && location.pathname.includes('/write/');
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
          <span>{online === null ? '连接中' : online ? `引擎 ${version ? `v${version}` : '在线'}` : '引擎离线'}</span>
        </div>
      </header>

      <Outlet context={{ project, refetchProjects: refetch } satisfies WorkspaceOutletContext} />
    </div>
  );
}
