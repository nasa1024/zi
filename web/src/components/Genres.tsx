export function Genres() {
  return (
    <section className="section genres" id="genres">
      <div className="wrap" style={{ textAlign: 'center' }}>
        <div className="reveal" style={{ display: 'inline-block' }}>
          <span className="eyebrow">FULL-SPECTRUM</span>
          <h2 className="cn" style={{ fontSize: 'clamp(2rem,4.6vw,3.2rem)', margin: '16px 0 12px' }}>一套引擎，<br />吃下全题材。</h2>
          <p style={{ color: 'var(--ink-soft)', maxWidth: '38em', margin: '0 auto 10px' }}>玄幻修仙的境界体系、都市言情的人物关系、悬疑无限流的伏笔回收——结构化 canon 通吃。</p>
        </div>
        <div className="pill-wall reveal">
          <span className="pill big c1"><span className="emo">⚔️</span> 玄幻</span>
          <span className="pill big c5"><span className="emo">🧙</span> 修仙</span>
          <span className="pill c2"><span className="emo">🏙️</span> 都市</span>
          <span className="pill c4"><span className="emo">🚀</span> 科幻</span>
          <span className="pill c5"><span className="emo">📜</span> 历史</span>
          <span className="pill c3"><span className="emo">🗡️</span> 武侠</span>
          <span className="pill c6"><span className="emo">💕</span> 言情</span>
          <span className="pill c5"><span className="emo">🔍</span> 悬疑</span>
          <span className="pill c1"><span className="emo">🎮</span> 游戏</span>
          <span className="pill c2"><span className="emo">♾️</span> 无限流</span>
          <span className="pill c4"><span className="emo">🐉</span> 西幻</span>
          <span className="pill c3"><span className="emo">☄️</span> 末世</span>
          <span className="pill c5"><span className="emo">👑</span> 宫斗</span>
          <span className="pill c6"><span className="emo">🌌</span> 星际</span>
        </div>
        <div className="tech-row reveal">
          <span className="tech-pill"><span className="gd"></span>Python 3.13</span>
          <span className="tech-pill"><span className="gd"></span>FastAPI</span>
          <span className="tech-pill"><span className="gd"></span>SQLite · WAL</span>
          <span className="tech-pill"><span className="gd"></span>FTS5 + jieba</span>
          <span className="tech-pill"><span className="gd"></span>SSE 流式</span>
          <span className="tech-pill"><span className="gd"></span>多供应商 LLM</span>
          <span className="tech-pill"><span className="gd"></span>SCHEMA v7</span>
        </div>
      </div>
    </section>
  );
}
