import { useEffect, useRef, useState } from 'react';

export default function Saga() {
  const trackRef = useRef<HTMLDivElement>(null);
  const [curScene, setCurScene] = useState(0);

  useEffect(() => {
    const track = trackRef.current;
    if (!track) return;

    function onScroll() {
      const el = trackRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const vh = window.innerHeight;
      const total = rect.height - vh;
      // 进度：0 当顶部贴住，1 当底部离开
      const prog = Math.min(Math.max(-rect.top / total, 0), 1);
      const idx = prog >= 0.66 ? 2 : prog >= 0.33 ? 1 : 0;
      setCurScene(idx);
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  return (
    <section className="saga" id="saga">
      <div className="saga-track" ref={trackRef}>
        <div className="saga-sticky">
          <div className="saga-progress">
            <div className={`node${curScene === 0 ? ' on' : ''}`} data-n="0"></div>
            <div className={`node${curScene === 1 ? ' on' : ''}`} data-n="1"></div>
            <div className={`node${curScene === 2 ? ' on' : ''}`} data-n="2"></div>
          </div>
          <div className="saga-inner">
            {/* 文案 */}
            <div className="saga-text">
              <div className={`saga-scene-txt${curScene === 0 ? ' on' : ''}`} data-n="0">
                <span className="saga-act a0">ACT 01 · 混乱 / CHAOS</span>
                <h3 className="cn">设定在脑子里<br />打架。</h3>
                <p>写到第 47 章——「等等，主角第 3 章不是炼气期吗？怎么这里成了筑基，下一章又掉回炼气？」</p>
                <p>跨越几十万字、几百个设定，人脑根本记不住。前后矛盾、人设崩塌、世界观漏洞，是连载写作最致命的「混乱」。</p>
                <div className="micro">❌ ch3: 炼气期 &nbsp;→&nbsp; ch47: 筑基期 &nbsp;→&nbsp; ch48: 炼气期 ??</div>
              </div>
              <div className={`saga-scene-txt${curScene === 1 ? ' on' : ''}`} data-n="1">
                <span className="saga-act a1">ACT 02 · 接管 / ENGINE</span>
                <h3 className="cn">引擎，<br />悄悄接住了。</h3>
                <p>每一条设定都进入只追加的 <b>canon 账本</b>。World State Store 用确定性 validator 做 as-of 投影——写第 48 章时，系统精确知道「此刻主角应是筑基」。</p>
                <p>矛盾在落笔前就被拦下，飞舞的散稿被收进秩序井然的状态表。</p>
                <div className="micro">✓ validator.power_rank · as_of=48 · status=canon</div>
              </div>
              <div className={`saga-scene-txt${curScene === 2 ? ' on' : ''}`} data-n="2">
                <span className="saga-act a2">ACT 03 · 和谐 / HARMONY</span>
                <h3 className="cn">读者一口气，<br />追到大结局。</h3>
                <p>前后自洽、伏笔回收、节奏带爽点。读者不再被低级错误劝退，而是沉浸在你的世界里疯狂追更。</p>
                <p>一致性是<b>不扣分项</b>，追更力是<b>得分项</b>——NovelForge 两手都抓。</p>
                <div className="micro">🔥 完读率 ↑ · 追更留存 ↑ · 弃书率 ↓</div>
              </div>
            </div>

            {/* 插画 */}
            <div className="saga-vis">
              {/* 场景0：焦头烂额的作者 */}
              <div className={`saga-scene-vis${curScene === 0 ? ' on' : ''}`} data-n="0">
                <svg viewBox="0 0 420 360" fill="none">
                  {/* 散乱矛盾纸张 */}
                  <g stroke="#181624" strokeWidth="3">
                    <rect x="20" y="40" width="74" height="54" rx="6" fill="#FF6A2C" transform="rotate(-14 57 67)" />
                    <rect x="320" y="30" width="74" height="54" rx="6" fill="#FF3D8B" transform="rotate(12 357 57)" />
                    <rect x="340" y="200" width="70" height="50" rx="6" fill="#FF6A2C" transform="rotate(-8 375 225)" />
                    <rect x="14" y="220" width="70" height="50" rx="6" fill="#FF3D8B" transform="rotate(10 49 245)" />
                  </g>
                  {/* 矛盾叹号 */}
                  <g fontFamily="DM Mono,monospace" fontSize="30" fill="#181624" fontWeight="bold">
                    <text x="44" y="76" transform="rotate(-14 57 67)">!?</text>
                    <text x="344" y="66" transform="rotate(12 357 57)">×</text>
                    <text x="360" y="234" transform="rotate(-8 375 225)">!!</text>
                    <text x="32" y="254" transform="rotate(10 49 245)">??</text>
                  </g>
                  {/* 角色：抱头 */}
                  <ellipse cx="210" cy="332" rx="120" ry="16" fill="#181624" opacity=".12" />
                  <path d="M150 320 Q146 230 210 228 Q274 230 270 320 Z" fill="#2F6BFF" stroke="#181624" strokeWidth="4" />
                  <circle cx="210" cy="180" r="48" fill="#C68642" stroke="#181624" strokeWidth="4" />
                  {/* 乱发 */}
                  <path d="M162 176 Q150 120 210 122 Q270 120 258 176 Q250 150 230 156 Q220 138 210 152 Q200 138 190 156 Q170 150 162 176Z" fill="#181624" />
                  {/* 抓头的手 */}
                  <path d="M170 150 Q150 120 168 96" stroke="#181624" strokeWidth="13" strokeLinecap="round" />
                  <path d="M170 150 Q150 120 168 96" stroke="#C68642" strokeWidth="7" strokeLinecap="round" />
                  <path d="M250 150 Q270 120 252 96" stroke="#181624" strokeWidth="13" strokeLinecap="round" />
                  <path d="M250 150 Q270 120 252 96" stroke="#C68642" strokeWidth="7" strokeLinecap="round" />
                  {/* 苦脸 */}
                  <path d="M192 178 l12 8 M228 178 l-12 8" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" />
                  <path d="M194 206 Q210 196 226 206" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" fill="none" />
                  {/* 头顶愤怒符号 */}
                  <text x="276" y="120" fontFamily="DM Mono" fontSize="26" fill="#FF3D8B" fontWeight="bold">#@!</text>
                </svg>
              </div>
              {/* 场景1：引擎接管 */}
              <div className={`saga-scene-vis${curScene === 1 ? ' on' : ''}`} data-n="1">
                <svg viewBox="0 0 420 360" fill="none">
                  {/* 收纳整齐的账本 */}
                  <g stroke="#181624" strokeWidth="3.5">
                    <rect x="250" y="70" width="140" height="210" rx="14" fill="#FBF1E3" />
                    <line x1="250" y1="110" x2="390" y2="110" />
                    <rect x="266" y="124" width="108" height="16" rx="4" fill="#C6F500" />
                    <rect x="266" y="150" width="88" height="14" rx="4" fill="#16D6B4" />
                    <rect x="266" y="174" width="100" height="14" rx="4" fill="#2F6BFF" />
                    <rect x="266" y="198" width="76" height="14" rx="4" fill="#FF6A2C" />
                    <rect x="266" y="222" width="94" height="14" rx="4" fill="#C6F500" />
                    <rect x="266" y="246" width="64" height="14" rx="4" fill="#FF3D8B" />
                  </g>
                  <text x="262" y="100" fontFamily="DM Mono" fontSize="13" fill="#181624">CANON LEDGER ✓</text>
                  {/* 机器人引擎角色 */}
                  <ellipse cx="130" cy="332" rx="100" ry="14" fill="#181624" opacity=".12" />
                  <rect x="74" y="200" width="112" height="120" rx="22" fill="#C6F500" stroke="#181624" strokeWidth="4" />
                  {/* 头 */}
                  <rect x="86" y="120" width="88" height="80" rx="20" fill="#FBF1E3" stroke="#181624" strokeWidth="4" />
                  {/* 天线 */}
                  <line x1="130" y1="120" x2="130" y2="98" stroke="#181624" strokeWidth="4" />
                  <circle cx="130" cy="92" r="9" fill="#FF3D8B" stroke="#181624" strokeWidth="3" />
                  {/* 眼睛（笑眼） */}
                  <path d="M104 156 Q112 148 120 156" stroke="#181624" strokeWidth="4" strokeLinecap="round" fill="none" />
                  <path d="M140 156 Q148 148 156 156" stroke="#181624" strokeWidth="4" strokeLinecap="round" fill="none" />
                  <path d="M118 174 Q130 182 142 174" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" fill="none" />
                  <circle cx="100" cy="170" r="5" fill="#FF6A2C" opacity=".5" />
                  <circle cx="160" cy="170" r="5" fill="#FF6A2C" opacity=".5" />
                  {/* 伸手把纸递进账本 */}
                  <path d="M186 230 Q230 220 248 188" stroke="#181624" strokeWidth="13" strokeLinecap="round" />
                  <rect x="206" y="186" width="40" height="30" rx="5" fill="#FF3D8B" stroke="#181624" strokeWidth="3" transform="rotate(-12 226 201)" />
                  <path d="M214 200 l6 6 12 -14" stroke="#FBF1E3" strokeWidth="3" fill="none" strokeLinecap="round" transform="rotate(-12 226 201)" />
                  {/* 校验对勾粒子 */}
                  <g stroke="#16D6B4" strokeWidth="3" strokeLinecap="round">
                    <path d="M198 120 l5 5 9 -11" />
                    <path d="M222 96 l5 5 9 -11" />
                  </g>
                </svg>
              </div>
              {/* 场景2：读者追更 */}
              <div className={`saga-scene-vis${curScene === 2 ? ' on' : ''}`} data-n="2">
                <svg viewBox="0 0 420 360" fill="none">
                  <ellipse cx="210" cy="334" rx="170" ry="16" fill="#181624" opacity=".1" />
                  {/* 读者1 */}
                  <g>
                    <path d="M70 322 Q66 256 116 256 Q166 256 162 322 Z" fill="#FF3D8B" stroke="#181624" strokeWidth="4" />
                    <circle cx="116" cy="214" r="36" fill="#F1C27D" stroke="#181624" strokeWidth="4" />
                    <path d="M82 212 Q78 174 116 174 Q154 174 150 212 Q140 192 116 196 Q92 192 82 212Z" fill="#181624" />
                    <path d="M104 214 Q110 208 116 214 M116 214 Q122 208 128 214" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" fill="none" />
                    <path d="M104 226 Q116 236 128 226" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" fill="none" />
                    <circle cx="98" cy="224" r="5" fill="#FF6A2C" opacity=".5" />
                    <circle cx="134" cy="224" r="5" fill="#FF6A2C" opacity=".5" />
                    {/* 手机 */}
                    <rect x="150" y="268" width="40" height="60" rx="8" fill="#181624" transform="rotate(-14 170 298)" />
                    <rect x="156" y="274" width="28" height="48" rx="4" fill="#C6F500" transform="rotate(-14 170 298)" />
                  </g>
                  {/* 读者2（戴眼镜，肤色不同） */}
                  <g>
                    <path d="M250 322 Q246 256 300 256 Q354 256 350 322 Z" fill="#2F6BFF" stroke="#181624" strokeWidth="4" />
                    <circle cx="300" cy="210" r="38" fill="#8D5524" stroke="#181624" strokeWidth="4" />
                    <path d="M262 208 Q258 166 300 166 Q342 166 338 208 Q344 182 300 180 Q256 182 262 208Z" fill="#181624" />
                    {/* 眼镜 */}
                    <circle cx="288" cy="210" r="9" fill="none" stroke="#181624" strokeWidth="3" />
                    <circle cx="314" cy="210" r="9" fill="none" stroke="#181624" strokeWidth="3" />
                    <line x1="297" y1="210" x2="305" y2="210" stroke="#181624" strokeWidth="3" />
                    <path d="M286 226 Q300 238 314 226" stroke="#181624" strokeWidth="3.5" strokeLinecap="round" fill="none" />
                    <rect x="232" y="266" width="40" height="58" rx="8" fill="#181624" transform="rotate(12 252 295)" />
                    <rect x="238" y="272" width="28" height="46" rx="4" fill="#FF6A2C" transform="rotate(12 252 295)" />
                  </g>
                  {/* 漂浮爱心 / 火 */}
                  <g>
                    <path d="M196 90 q-14 -18 -28 -4 q-14 14 28 40 q42 -26 28 -40 q-14 -14 -28 4z" fill="#FF3D8B" stroke="#181624" strokeWidth="3" />
                    <text x="120" y="120" fontSize="30">🔥</text>
                    <text x="276" y="110" fontSize="26">✨</text>
                    <path d="M236 150l4 11 11 4-11 4-4 11-4-11-11-4 11-4z" fill="#C6F500" stroke="#181624" strokeWidth="2" />
                  </g>
                </svg>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
