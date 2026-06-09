export default function Footer() {
  return (
    <footer className="foot" id="docs">
      <div className="wrap">
        <div className="foot-top">
          <div className="foot-brand">
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
            <p>
              面向网文 / 长篇连载的 AI 写作「记忆 + 一致性 + 工艺」引擎。本地优先，单一 SQLite，单人可维护。
            </p>
          </div>
          <div className="foot-cols">
            <div className="foot-col">
              <h5>产品</h5>
              <a href="#saga">情绪滑梯</a>
              <a href="#pillars">三大支柱</a>
              <a href="#pipeline">写作流水线</a>
              <a href="#modes">双模式</a>
            </div>
            <div className="foot-col">
              <h5>文档</h5>
              <a href="#">设计文档 §00–14</a>
              <a href="#">实现规格 impl/</a>
              <a href="#">REST API 一览</a>
              <a href="#">README</a>
            </div>
            <div className="foot-col">
              <h5>开发</h5>
              <a href="#">GitHub</a>
              <a href="#">快速开始</a>
              <a href="#">迁移指南</a>
              <a href="#">贡献</a>
            </div>
          </div>
        </div>
        <div className="foot-bottom">
          <span>© 2026 NovelForge · 让冷引擎写出滚烫的故事</span>
          <span>MVP v0.3 · 262 tests green · made with ⚡ &amp; dopamine</span>
        </div>
      </div>
    </footer>
  );
}
