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
    title: '人工审核',
    tag: '每一条新设定都等你点头',
    lines: [
      ['same', '规划、召回、起草、校验流程完全一致'],
      ['gate', '低风险新设定也进入审核队列'],
      ['gate', '中 / 高风险必须人工批准'],
      ['gate', '你决定哪些设定可以被确认'],
    ],
  },
  auto: {
    title: '自动确认',
    tag: '低风险设定自动晋升，你只管写',
    lines: [
      ['same', '规划、召回、起草、校验流程完全一致'],
      ['auto', '低风险新设定自动成为已确认设定'],
      ['auto', '中风险按人机协作规则自动处理'],
      ['gate', '仅高风险才停下等你'],
    ],
  },
};

const labelMap: Record<BadgeKey, string> = { auto: '自动确认', gate: '人工审核', same: '共同流程' };

export function Modes() {
  const [active, setActive] = useState<ModeKey>('gate');
  const m = MODES[active];

  return (
    <section className="section" id="modes">
      <div className="wrap modes-grid">
        <div className="switch-wrap reveal">
          <span className="eyebrow" style={{ marginBottom: 18 }}>两种审核方式</span>
          <h2 className="cn" style={{ fontSize: '2.6rem', margin: '14px 0 26px' }}>一处设置，<br />两种活法。</h2>
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
          <p style={{ color: 'var(--ink-soft)', maxWidth: '24em', margin: '0 auto' }}>同一套写作流程、同一份已确认设定档案。区别只在新设定由你逐条确认，还是由系统按风险自动处理。</p>
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
