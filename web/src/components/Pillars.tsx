export default function Pillars() {
  return (
    <section className="section" id="pillars">
      <div className="wrap">
        <div className="sec-head reveal">
          <span className="eyebrow">THREE PILLARS</span>
          <h2 className="cn">三大支柱，<br />缺一不可。</h2>
          <p>记忆让它「记得住」，一致性让它「不出错」，工艺让它「更好看」。三者正交，各司其职。</p>
        </div>
        <div className="pillars-grid">
          <div className="pillar p1 reveal">
            <span className="num">01</span>
            <svg className="picon" viewBox="0 0 64 64" fill="none">
              <rect x="10" y="14" width="44" height="38" rx="8" fill="#FBF1E3" stroke="#181624" strokeWidth="4" />
              <line x1="10" y1="26" x2="54" y2="26" stroke="#181624" strokeWidth="3" />
              <rect x="18" y="34" width="20" height="5" rx="2.5" fill="#FF3D8B" />
              <rect x="18" y="42" width="28" height="5" rx="2.5" fill="#2F6BFF" />
              <circle cx="48" cy="14" r="9" fill="#FF6A2C" stroke="#181624" strokeWidth="3" />
            </svg>
            <h3 className="cn">记忆</h3>
            <p>分层记忆 + 实体优先召回。软记忆走 RAG，硬状态进关系表。要用什么设定，它先帮你捞回来。</p>
            <div className="tags"><span>实体召回</span><span>FTS5 检索</span><span>World Bible</span></div>
          </div>
          <div className="pillar p2 reveal">
            <span className="num">02</span>
            <svg className="picon" viewBox="0 0 64 64" fill="none">
              <circle cx="32" cy="32" r="24" fill="#FBF1E3" stroke="#181624" strokeWidth="4" />
              <path d="M22 32 l7 8 14 -17" stroke="#16D6B4" strokeWidth="6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="32" cy="32" r="30" stroke="#181624" strokeWidth="2" strokeDasharray="4 5" fill="none" />
            </svg>
            <h3 className="cn">一致性</h3>
            <p>World State + 确定性 Python validator + as-of 投影。硬冲突落笔前拦截，软冲突 LLM 二次判断。</p>
            <div className="tags"><span>canon 账本</span><span>as-of 投影</span><span>确定性校验</span></div>
          </div>
          <div className="pillar p3 reveal">
            <span className="num">03</span>
            <svg className="picon" viewBox="0 0 64 64" fill="none">
              <path d="M32 6l7 18 19 1-15 12 5 19-16-11-16 11 5-19-15-12 19-1z" fill="#C6F500" stroke="#181624" strokeWidth="4" strokeLinejoin="round" />
            </svg>
            <h3 className="cn">工艺</h3>
            <p>爽点、钩子、节奏、value_shift。一致性是不扣分项，追更力才是得分项——这一层专管「好看」。</p>
            <div className="tags"><span>爽点节奏</span><span>章末钩子</span><span>价值轴</span></div>
          </div>
        </div>
      </div>
    </section>
  );
}
