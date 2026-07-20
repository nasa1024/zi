import { useCallback, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useHealth, useProjects } from '../api/hooks';
import type { ProjectResponse } from '../api/types';
import CreateProjectModal from '../components/studio/CreateProjectModal';
import '../styles/studio.css';
import '../styles/studio-page.css';
import '../styles/workspace.css';

function projectProfileLabel(project: ProjectResponse): string {
  if (
    project.writing_profile_summary
    && typeof project.writing_profile_summary === 'object'
    && project.writing_profile_summary.display_name
  ) {
    return project.writing_profile_summary.display_name;
  }
  return project.writing_profile_name ?? project.writing_profile_id ?? 'cn_web_novel_v1';
}

function StudioNav(): JSX.Element {
  const { online, version } = useHealth();
  const label = online === null ? '连接中…' : online ? (version ? `v${version}` : '在线') : '离线';
  return (
    <nav className="studio-nav">
      <div className="wrap sn-inner">
        <Link to="/" className="sn-back"><span className="sn-back-arrow">←</span><span>主页</span></Link>
        <div className="sn-brand"><span className="spark">✦</span>zi<span className="sn-label">STUDIO</span></div>
        <div className="sn-health"><span className={`picker-health-dot${online ? ' online' : ''}`} />{label}</div>
      </div>
    </nav>
  );
}

export default function StudioPage(): JSX.Element {
  const navigate = useNavigate();
  const [modalOpen, setModalOpen] = useState(false);
  const { projects, loading, error, refetch, addProject } = useProjects();

  const handleCreated = useCallback((project: ProjectResponse) => {
    addProject(project);
    setModalOpen(false);
    refetch();
    navigate(`/studio/${encodeURIComponent(project.project_id)}/plan`);
  }, [addProject, navigate, refetch]);

  return (
    <>
      <StudioNav />
      <main className="studio-picker-page">
        <div className="studio-picker-intro">
          <div>
            <span>SELECT A MANUSCRIPT</span>
            <h1>今天继续锻哪一本？</h1>
            <p>项目位置写进 URL。复制链接、刷新页面或打开多个标签，都不会串到另一本书。</p>
          </div>
          <button type="button" className="picker-new-book" onClick={() => setModalOpen(true)}>＋ 新建一本书</button>
        </div>

        {error && <div className="workspace-banner" role="alert">{error}</div>}
        {loading ? (
          <div className="workspace-loading"><span className="nf-spin" /> 正在清点书架…</div>
        ) : projects.length === 0 ? (
          <section className="studio-picker-empty">
            <span>EMPTY SHELF</span>
            <h2>书架还空着</h2>
            <p>先建立全书 premise，再进入规划室选择第一条故事路线。</p>
            <button type="button" onClick={() => setModalOpen(true)}>开始第一本书</button>
          </section>
        ) : (
          <div className="project-bookshelf">
            {projects.map((project, index) => (
              <article className="project-book" key={project.project_id}>
                <div className="project-book-index">NF / {String(index + 1).padStart(2, '0')}</div>
                <span className="project-book-genre">{project.primary_genre || project.genre}</span>
                <h2>{project.name}</h2>
                <p>{project.premise || '这本书还在等待一句清晰的核心命题。'}</p>
                <div className="project-book-profile">
                  <span>{project.content_language ?? 'zh-CN'}</span>
                  <span>{projectProfileLabel(project)}</span>
                </div>
                <dl>
                  <div><dt>章节</dt><dd>{project.chapter_count}</dd></div>
                  <div><dt>Canon</dt><dd>{project.canon_fact_count}</dd></div>
                </dl>
                <div className="project-book-actions">
                  <button type="button" onClick={() => navigate(`/studio/${encodeURIComponent(project.project_id)}/plan`)}>看规划</button>
                  <button type="button" className="primary" onClick={() => navigate(`/studio/${encodeURIComponent(project.project_id)}/write/${Math.max(1, project.chapter_count + 1)}`)}>继续写作 →</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </main>
      <CreateProjectModal open={modalOpen} onClose={() => setModalOpen(false)} onCreated={handleCreated} />
    </>
  );
}
