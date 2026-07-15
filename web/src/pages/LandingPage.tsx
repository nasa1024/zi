// 落地页：营销官网（霓虹蛮荒多巴胺风格）。
// 新建项目后直接跳转到 URL 驱动的项目规划室。

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import CTA from '../components/CTA';
import Footer from '../components/Footer';
import { Genres } from '../components/Genres';
import Hero from '../components/Hero';
import Marquee from '../components/Marquee';
import { Modes } from '../components/Modes';
import Nav from '../components/Nav';
import { Pipeline } from '../components/Pipeline';
import Pillars from '../components/Pillars';
import Saga from '../components/Saga';
import Stats from '../components/Stats';
import CreateProjectModal from '../components/studio/CreateProjectModal';
import type { ProjectResponse } from '../api/types';

export default function LandingPage(): JSX.Element {
  const navigate = useNavigate();
  const [modalOpen, setModalOpen] = useState<boolean>(false);

  const openModal = useCallback(() => setModalOpen(true), []);
  const closeModal = useCallback(() => setModalOpen(false), []);

  const handleCreated = useCallback(
    (p: ProjectResponse) => {
      setModalOpen(false);
      navigate(`/studio/${encodeURIComponent(p.project_id)}/plan`);
    },
    [navigate],
  );

  // reveal-on-scroll
  useEffect(() => {
    const els = document.querySelectorAll<HTMLElement>('.reveal');
    if (els.length === 0) return;
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
        });
      },
      { threshold: 0.16 },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  return (
    <>
      <Nav onStart={openModal} />
      <main>
        <Hero onStart={openModal} />
        <Marquee />
        <Saga />
        <Pillars />
        <Pipeline />
        <Modes />
        <Genres />
        <Stats />
        <CTA onStart={openModal} />
      </main>
      <Footer />
      <CreateProjectModal open={modalOpen} onClose={closeModal} onCreated={handleCreated} />
    </>
  );
}
