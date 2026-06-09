import { useState } from 'react';

type ModeKey = 'gate' | 'auto';
type BadgeKey = 'auto' | 'gate' | 'same';

interface ModeData {
  title: string;
  tag: string;
  lines: [BadgeKey, string][];
}

const MODES: Record<ModeKey, ModeData> = {
  gate: {
    title: 'human_gate · 人审模式',
    tag: '每一条新设定都等你点头',
    lines: [
      ['same', 'plan / recall / draft / check 完全一致'],
      ['gate', '低风险 fact 也进审核队列'],
      ['gate', '中 / 高风险必须人工批准'],
      ['gate', '你拥有 canon 的最终决定权'],
    ],
  },
  auto: {
    title: 'auto_promote · 全自动',
    tag: '低风险设定自动晋升，你只管写',
    lines: [
      ['same', 'plan / recall / draft / check 完全一致'],
      ['auto', '低风险 fact 直接晋升 canon'],
      ['auto', '中风险按 hybrid 策略自动放行'],
      ['gate', '仅高风险才停下等你'],
    ],
  },
};

const labelMap: Record<BadgeKey, string> = { auto: 'AUTO', gate: 'GATE', same: 'SAME' };

export function Modes() {
  const [active, setActive] = useState<ModeKey>('gate');
  const m = MODES[active];

  return (
    <section className="section" id="modes">
      <div className="wrap modes-grid">
        <div className="switch-wrap reveal">
          <span className="eyebrow" style={{ marginBottom: 18 }}>DUAL MODE</span>
          <h2 className="cn" style={{ fontSize: '2.6rem', margin: '14px 0 26px' }}>一行 config，<br />两种活法。</h2>
          <div className="switch">
            <button
              className={active === 'gate' ? 'act' : undefined}
              data-mode="gate"
              onClick={() => setActive('gate')}
            >🧑‍⚖️ 人审模式</button>
            <button
              className={active === 'auto' ? 'act' : undefined}
              data-mode="auto"
              onClick={() => setActive('auto')}
            >🤖 全自动</button>
          </div>
          <p style={{ color: 'var(--ink-soft)', maxWidth: '24em', margin: '0 auto' }}>同一条管线、同一套 canon 账本。区别只在那道「晋升闸门」放不放行。</p>
        </div>

        <div className="mode-panel reveal">
          <h3 id="modeTitle">{m.title}</h3>
          <div className="tagline" id="modeTag">{m.tag}</div>
          <div id="modeLines">
            {m.lines.map(([b, t], i) => (
              <div className="mode-line" key={i}>
                <span className={`badge ${b}`}>{labelMap[b]}</span>
                <span>{t}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
