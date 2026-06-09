// 工作台独立页面：/studio
// 有自己的顶栏（返回主页 + 健康徽章），不含落地页营销内容。
// activeProjectId 通过 localStorage 从落地页（新建项目流）持久化过来。

import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { useHealth } from '../api/hooks';
import { useProjects } from '../api/hooks';
import type { ProjectResponse } from '../api/types';
import CreateProjectModal from '../components/studio/CreateProjectModal';
import Studio from '../components/studio/Studio';
import '../styles/studio-page.css';

const ACTIVE_PROJECT_STORAGE = 'nf_active_project';

function readActiveProject(): string | null {
  try { return localStorage.getItem(ACTIVE_PROJECT_STORAGE); } catch { return null; }
}

function writeActiveProject(id: string | null): void {
  try {
    if (id) localStorage.setItem(ACTIVE_PROJECT_STORAGE, id);
    else localStorage.removeItem(ACTIVE_PROJECT_STORAGE);
  } catch { /* ignore */ }
}

function StudioNav(): JSX.Element {
  const { online, version } = useHealth();

  let dotColor: string;
  let label: string;
  if (online === null) {
    dotColor = 'var(--ink-soft)'; label = '连接中…';
  } else if (online) {
    dotColor = 'var(--lime)'; label = version ? `v${version}` : '在线';
  } else {
    dotColor = 'var(--orange)'; label = '离线';
  }

  return (
    <nav className="studio-nav">
      <div className="wrap sn-inner">
        <Link to="/" className="sn-back">
          <span className="sn-back-arrow">←</span>
          <span>主页</span>
        </Link>
        <div className="sn-brand">
          <span className="spark" style={{ display: 'inline-flex', marginRight: 6 }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
              <path
                d="M12 2l2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4L12 2z"
                fill="#FBF1E3"
                stroke="#181624"
                strokeWidth="1.5"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          NovelForge
          <span className="sn-label">STUDIO</span>
        </div>
        <div className="sn-health">
          <span className="dot" style={{ background: dotColor, width: 8, height: 8, borderRadius: '50%', display: 'inline-block', marginRight: 6 }} />
          <span>{label}</span>
        </div>
      </div>
    </nav>
  );
}

export default function StudioPage(): JSX.Element {
  const [activeProjectId, setActiveProjectId] = useState<string | null>(() =>
    readActiveProject(),
  );
  const [modalOpen, setModalOpen] = useState<boolean>(false);

  const {
    projects,
    loading: projectsLoading,
    error: projectsError,
    refetch: refetchProjects,
    addProject,
  } = useProjects();

  const selectProject = useCallback((id: string | null) => {
    setActiveProjectId(id);
    writeActiveProject(id);
  }, []);

  const openModal = useCallback(() => setModalOpen(true), []);
  const closeModal = useCallback(() => setModalOpen(false), []);

  const handleCreated = useCallback(
    (p: ProjectResponse) => {
      addProject(p);
      selectProject(p.project_id);
      setModalOpen(false);
      refetchProjects();
    },
    [addProject, selectProject, refetchProjects],
  );

  return (
    <>
      <StudioNav />
      <Studio
        activeProjectId={activeProjectId}
        onSelectProject={selectProject}
        onRequestCreate={openModal}
        projects={projects}
        projectsLoading={projectsLoading}
        projectsError={projectsError}
        onRefetchProjects={refetchProjects}
      />
      <CreateProjectModal open={modalOpen} onClose={closeModal} onCreated={handleCreated} />
    </>
  );
}
