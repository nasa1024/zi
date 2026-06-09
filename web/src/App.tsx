// App — 集成落地页 + 工作台。
// 复刻 design-reference.html 的渲染顺序与全局 reveal 脚本；
// 持有跨组件的全局状态：activeProjectId（持久化到 localStorage）+ modalOpen。

import { useCallback, useEffect, useState } from 'react';
import Nav from './components/Nav';
import Hero from './components/Hero';
import Marquee from './components/Marquee';
import Saga from './components/Saga';
import Pillars from './components/Pillars';
import { Pipeline } from './components/Pipeline';
import { Modes } from './components/Modes';
import { Genres } from './components/Genres';
import Stats from './components/Stats';
import CTA from './components/CTA';
import Footer from './components/Footer';
import Studio from './components/studio/Studio';
import CreateProjectModal from './components/studio/CreateProjectModal';
import { useProjects } from './api/hooks';
import type { ProjectResponse } from './api/types';

const ACTIVE_PROJECT_STORAGE = 'nf_active_project';

function readActiveProject(): string | null {
  try {
    return localStorage.getItem(ACTIVE_PROJECT_STORAGE);
  } catch {
    return null;
  }
}

function writeActiveProject(id: string | null): void {
  try {
    if (id) {
      localStorage.setItem(ACTIVE_PROJECT_STORAGE, id);
    } else {
      localStorage.removeItem(ACTIVE_PROJECT_STORAGE);
    }
  } catch {
    // localStorage unavailable — ignore.
  }
}

function scrollToStudio(): void {
  document.getElementById('studio')?.scrollIntoView({ behavior: 'smooth' });
}

export default function App(): JSX.Element {
  const [activeProjectId, setActiveProjectId] = useState<string | null>(() =>
    readActiveProject(),
  );
  const [modalOpen, setModalOpen] = useState<boolean>(false);

  // 项目列表上提到 App：新建后可即时（乐观）注入并对账刷新，
  // 让 Studio 立刻拿到新项目，消除“创建成功但列表未刷新”的空态闪烁。
  const {
    projects,
    loading: projectsLoading,
    error: projectsError,
    refetch: refetchProjects,
    addProject,
  } = useProjects();

  // activeProjectId setter that also persists to localStorage.
  const selectProject = useCallback((id: string | null) => {
    setActiveProjectId(id);
    writeActiveProject(id);
  }, []);

  const openModal = useCallback(() => setModalOpen(true), []);
  const closeModal = useCallback(() => setModalOpen(false), []);

  const handleCreated = useCallback(
    (p: ProjectResponse) => {
      addProject(p); // 乐观注入 → active 立即解析到新项目
      selectProject(p.project_id);
      setModalOpen(false);
      scrollToStudio();
      refetchProjects(); // 后台与后端对账（拿到真实 stats 等）
    },
    [addProject, selectProject, refetchProjects],
  );

  // 全局 reveal-on-scroll：复刻 design-reference.html 的脚本（threshold .16）。
  useEffect(() => {
    const els = document.querySelectorAll<HTMLElement>('.reveal');
    if (els.length === 0) return;

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add('in');
            io.unobserve(e.target);
          }
        });
      },
      { threshold: 0.16 },
    );

    els.forEach((el) => io.observe(el));

    return () => io.disconnect();
  }, []);

  return (
    <>
      <Nav onStart={openModal} onOpenStudio={scrollToStudio} />
      <main>
        <Hero onStart={openModal} />
        <Marquee />
        <Saga />
        <Pillars />
        <Pipeline />
        <Modes />
        <Genres />
        <Stats />
        <Studio
          activeProjectId={activeProjectId}
          onSelectProject={selectProject}
          onRequestCreate={openModal}
          projects={projects}
          projectsLoading={projectsLoading}
          projectsError={projectsError}
          onRefetchProjects={refetchProjects}
        />
        <CTA onStart={openModal} />
      </main>
      <Footer />
      <CreateProjectModal open={modalOpen} onClose={closeModal} onCreated={handleCreated} />
    </>
  );
}
