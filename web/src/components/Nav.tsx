// Nav — restores <nav class="nav"> from design-reference.html.
// The hero-tag style status dot is repurposed into a live health badge via useHealth().
// See SHARED CONTRACT.

import { useHealth } from '../api/hooks';

export interface NavProps {
  onStart: () => void;
  onOpenStudio: () => void;
}

export default function Nav({ onStart, onOpenStudio }: NavProps) {
  const { online, version } = useHealth();

  // online===null → 连接中 (grey) · true → 引擎在线 vX.Y (lime) · false → 引擎离线 (orange)
  let dotColor: string;
  let label: string;
  if (online === null) {
    dotColor = 'var(--ink-soft)';
    label = '连接中…';
  } else if (online) {
    dotColor = 'var(--lime)';
    label = version ? `引擎在线 v${version}` : '引擎在线';
  } else {
    dotColor = 'var(--orange)';
    label = '引擎离线';
  }

  return (
    <nav className="nav">
      <div className="wrap nav-in">
        <a href="#top" className="brand">
          <span className="spark">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
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
        </a>
        <div className="nav-links">
          <a href="#saga">情绪滑梯</a>
          <a href="#pillars">三大支柱</a>
          <a href="#pipeline">写作流水线</a>
          <a href="#genres">题材覆盖</a>
          <a
            href="#studio"
            onClick={(e) => {
              e.preventDefault();
              onOpenStudio();
            }}
          >
            工作台
          </a>
          <a href="#docs">文档</a>
        </div>
        <div className="hero-tag" style={{ marginBottom: 0 }}>
          <span className="dot" style={{ background: dotColor }} />
          {label}
        </div>
        <a
          href="#cta"
          className="btn nav-cta pink"
          onClick={(e) => {
            e.preventDefault();
            onStart();
          }}
        >
          开始创作 →
        </a>
        <button
          className="burger"
          aria-label="menu"
          onClick={() => {
            document.querySelector('#studio')?.scrollIntoView({ behavior: 'smooth' });
          }}
        >
          ☰
        </button>
      </div>
    </nav>
  );
}
