// Hero — restores <section class="hero"> from design-reference.html.
// "⚡ 立即开写" CTA calls onStart; "看它怎么救场 ↓" keeps href="#saga".
// See SHARED CONTRACT.

export interface HeroProps {
  onStart: () => void;
}

export default function Hero({ onStart }: HeroProps) {
  return (
    <section className="hero">
      <div className="wrap hero-grid">
        <div className="hero-copy">
          <div className="hero-tag">
            <span className="dot" /> AI 网文写作引擎 · MVP v0.3
          </div>
          <h1>
            <span>
              让<span className="hi-blue">冷</span>引擎，
            </span>
            <span className="cn">
              写出
              <span className="hi-underline">
                滚烫的故事
                <svg viewBox="0 0 200 12" preserveAspectRatio="none">
                  <path
                    d="M2 8 Q50 2 100 7 T198 5"
                    stroke="#FF3D8B"
                    strokeWidth="5"
                    fill="none"
                    strokeLinecap="round"
                  />
                </svg>
              </span>
            </span>
          </h1>
          <p className="hero-sub">
            SQLite 账本、确定性 validator、as-of 投影 —— 听起来很硬核？
            它们只为一件事服务：让你的主角<b>境界不倒退</b>、伏笔<b>必回收</b>、世界观<b>永不崩塌</b>，
            同时把每一章都写出<b>追更的爽感</b>。
          </p>
          <div className="hero-cta">
            <a
              href="#cta"
              className="btn"
              onClick={(e) => {
                e.preventDefault();
                onStart();
              }}
            >
              ⚡ 立即开写
            </a>
            <a href="#saga" className="btn ghost">
              看它怎么救场 ↓
            </a>
          </div>
          <div className="hero-foot">
            <div className="stat">
              <b>262</b>
              <span>测试全绿</span>
            </div>
            <div className="stat">
              <b>0</b>
              <span>外网依赖建库</span>
            </div>
            <div className="stat">
              <b>2</b>
              <span>模式一键切换</span>
            </div>
            <div className="stat">
              <b>∞</b>
              <span>章节不崩</span>
            </div>
          </div>
        </div>

        {/* 插画舞台 */}
        <div className="stage reveal">
          <div className="stage-bg">
            <span
              className="blob"
              style={{
                width: '160px',
                height: '160px',
                background: 'var(--pink)',
                top: '-40px',
                right: '-30px',
              }}
            />
            <span
              className="blob"
              style={{
                width: '120px',
                height: '120px',
                background: 'var(--lime)',
                bottom: '-30px',
                left: '-20px',
              }}
            />
            <svg
              className="deco slow"
              style={{ top: '18px', left: '24px', width: '40px', height: '40px' }}
              viewBox="0 0 40 40"
            >
              <path
                d="M20 2l4 12 12 4-12 4-4 12-4-12-12-4 12-4z"
                fill="#C6F500"
                stroke="#181624"
                strokeWidth="2"
              />
            </svg>
            <svg
              className="deco"
              style={{ bottom: '22px', right: '30px', width: '34px', height: '34px' }}
              viewBox="0 0 34 34"
            >
              <circle cx="17" cy="17" r="14" fill="#FF6A2C" stroke="#181624" strokeWidth="2.5" />
            </svg>
          </div>

          {/* 中央创作小人：拿魔法笔的角色 */}
          <svg className="hero-char" viewBox="0 0 300 320" fill="none">
            {/* 书桌底座 */}
            <rect x="40" y="250" width="220" height="44" rx="12" fill="#181624" />
            <rect
              x="40"
              y="244"
              width="220"
              height="14"
              rx="7"
              fill="#FBF1E3"
              stroke="#181624"
              strokeWidth="3"
            />
            {/* 身体 */}
            <path
              d="M110 250 Q108 180 150 178 Q192 180 190 250 Z"
              fill="#FF3D8B"
              stroke="#181624"
              strokeWidth="4"
            />
            {/* 围巾/领 */}
            <path
              d="M126 190 Q150 206 174 190 L168 210 Q150 220 132 210 Z"
              fill="#C6F500"
              stroke="#181624"
              strokeWidth="3"
            />
            {/* 头 */}
            <circle cx="150" cy="146" r="40" fill="#F1C27D" stroke="#181624" strokeWidth="4" />
            {/* 头发 */}
            <path
              d="M110 142 Q108 100 150 100 Q192 100 190 142 Q180 120 150 122 Q120 120 110 142Z"
              fill="#181624"
            />
            {/* 眼睛 */}
            <circle cx="137" cy="148" r="4.5" fill="#181624" />
            <circle cx="165" cy="148" r="4.5" fill="#181624" />
            {/* 腮红 */}
            <circle cx="128" cy="160" r="6" fill="#FF6A2C" opacity=".5" />
            <circle cx="172" cy="160" r="6" fill="#FF6A2C" opacity=".5" />
            {/* 微笑 */}
            <path
              d="M140 164 Q150 174 160 164"
              stroke="#181624"
              strokeWidth="3.5"
              strokeLinecap="round"
              fill="none"
            />
            {/* 手臂 + 魔法笔 */}
            <path
              d="M186 212 Q224 200 236 168"
              stroke="#181624"
              strokeWidth="14"
              strokeLinecap="round"
            />
            <path
              d="M186 212 Q224 200 236 168"
              stroke="#F1C27D"
              strokeWidth="8"
              strokeLinecap="round"
            />
            <rect
              x="228"
              y="150"
              width="10"
              height="34"
              rx="5"
              transform="rotate(28 233 167)"
              fill="#2F6BFF"
              stroke="#181624"
              strokeWidth="3"
            />
            {/* 笔尖星光 */}
            <path
              d="M246 150l3 8 8 3-8 3-3 8-3-8-8-3 8-3z"
              fill="#C6F500"
              stroke="#181624"
              strokeWidth="1.6"
            />
            {/* 左手扶书 */}
            <path
              d="M114 212 Q90 214 84 244"
              stroke="#181624"
              strokeWidth="14"
              strokeLinecap="round"
            />
            <path
              d="M114 212 Q90 214 84 244"
              stroke="#F1C27D"
              strokeWidth="8"
              strokeLinecap="round"
            />
            {/* 桌上发光的书 */}
            <rect
              x="70"
              y="232"
              width="64"
              height="20"
              rx="4"
              fill="#16D6B4"
              stroke="#181624"
              strokeWidth="3.5"
            />
            <line x1="102" y1="232" x2="102" y2="252" stroke="#181624" strokeWidth="2.5" />
          </svg>

          {/* 漂浮设定卡 */}
          <div className="float-card fc1" style={{ ['--r' as any]: '-5deg' }}>
            <span className="ic" style={{ background: 'var(--pink)' }}>
              ⚔️
            </span>{' '}
            境界 · 炼气→筑基
          </div>
          <div className="float-card fc2" style={{ ['--r' as any]: '4deg' }}>
            <span className="ic" style={{ background: 'var(--lime)' }}>
              ✓
            </span>{' '}
            一致性 PASS
          </div>
          <div className="float-card fc3" style={{ ['--r' as any]: '6deg' }}>
            <span className="ic" style={{ background: 'var(--orange)' }}>
              🧩
            </span>{' '}
            伏笔 #07 已回收
          </div>
          <div className="float-card fc4" style={{ ['--r' as any]: '-4deg' }}>
            <span className="ic" style={{ background: 'var(--blue)', color: '#fff' }}>
              🔥
            </span>{' '}
            本章爽点 ×3
          </div>
        </div>
      </div>
    </section>
  );
}
