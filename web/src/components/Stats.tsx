import { useEffect, useRef } from 'react';

export default function Stats() {
  const gridRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = gridRef.current;
    if (!root) return;

    const counters = [...root.querySelectorAll<HTMLElement>('[data-count]')];
    const timers: ReturnType<typeof setInterval>[] = [];

    const cio = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          const el = e.target as HTMLElement;
          const target = +(el.dataset.count ?? '0');
          let cur = 0;
          const step = Math.max(1, Math.round(target / 40));
          const t = setInterval(() => {
            cur += step;
            if (cur >= target) {
              cur = target;
              clearInterval(t);
            }
            el.textContent = String(cur);
          }, 28);
          timers.push(t);
          cio.unobserve(el);
        });
      },
      { threshold: 0.6 }
    );

    counters.forEach((c) => cio.observe(c));

    return () => {
      cio.disconnect();
      timers.forEach((t) => clearInterval(t));
    };
  }, []);

  return (
    <section className="section" style={{ paddingTop: 40 }}>
      <div className="wrap">
        <div className="stats-grid reveal" ref={gridRef}>
          <div className="stat-cell">
            <b data-count="262">0</b>
            <span>测试用例全绿</span>
          </div>
          <div className="stat-cell">
            <b data-count="7">0</b>
            <span>SCHEMA 版本</span>
          </div>
          <div className="stat-cell">
            <b data-count="5">0</b>
            <span>核心 Skill</span>
          </div>
          <div className="stat-cell">
            <b data-count="13">0</b>
            <span>功能组落地</span>
          </div>
        </div>
      </div>
    </section>
  );
}
