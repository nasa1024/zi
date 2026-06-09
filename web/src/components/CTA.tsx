interface CTAProps {
  onStart: () => void;
}

export default function CTA({ onStart }: CTAProps) {
  return (
    <section className="cta" id="cta">
      <div className="wrap">
        <div className="cta-box reveal">
          <svg
            className="cta-deco"
            style={{ top: 24, left: 30, width: 48 }}
            viewBox="0 0 48 48"
          >
            <path
              d="M24 2l6 16 16 6-16 6-6 16-6-16-16-6 16-6z"
              fill="#C6F500"
              stroke="#181624"
              strokeWidth="3"
            />
          </svg>
          <svg
            className="cta-deco"
            style={{ bottom: 24, right: 34, width: 40 }}
            viewBox="0 0 40 40"
          >
            <circle
              cx="20"
              cy="20"
              r="16"
              fill="#2F6BFF"
              stroke="#181624"
              strokeWidth="3"
            />
          </svg>
          <span className="eyebrow" style={{ color: 'var(--cream)', justifyContent: 'center' }}>
            START NOW
          </span>
          <h2>
            准备好让你的世界观<br />
            <span className="cn">永不崩塌</span>了吗？
          </h2>
          <p>本地优先、零外网即可建库与跑测。一条 uvicorn 命令，引擎就位。</p>
          <div className="hero-cta">
            <button type="button" className="btn" onClick={onStart}>
              ⚡ 开始创作
            </button>
            <a
              href="#docs"
              className="btn ghost"
              style={{ background: 'var(--ink)', color: 'var(--cream)', borderColor: 'var(--cream)' }}
            >
              📖 读文档
            </a>
          </div>
          <div
            style={{
              marginTop: 30,
              display: 'inline-block',
              background: 'var(--ink)',
              color: 'var(--lime)',
              fontFamily: 'var(--font-mono)',
              fontSize: '.82rem',
              padding: '.8em 1.2em',
              borderRadius: 12,
              boxShadow: 'var(--shadow-sm)',
              border: 'var(--bd)',
            }}
          >
            $ uvicorn novelforge.app.main:app --port 8787
          </div>
        </div>
      </div>
    </section>
  );
}
