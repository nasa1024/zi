// Marquee — restores <div class="marquee"> from design-reference.html.
// Two identical repeated spans for the seamless infinite scroll.
// See SHARED CONTRACT.

export default function Marquee() {
  return (
    <div className="marquee">
      <div className="marquee-track">
        <span>
          确定性一致性 <i className="star">✦</i> 分层记忆召回 <i className="star">✦</i> 双模式治理{' '}
          <i className="star">✦</i> 追更力工艺层 <i className="star">✦</i> as-of 时点投影{' '}
          <i className="star">✦</i> 伏笔必回收 <i className="star">✦</i>
        </span>
        <span>
          确定性一致性 <i className="star">✦</i> 分层记忆召回 <i className="star">✦</i> 双模式治理{' '}
          <i className="star">✦</i> 追更力工艺层 <i className="star">✦</i> as-of 时点投影{' '}
          <i className="star">✦</i> 伏笔必回收 <i className="star">✦</i>
        </span>
      </div>
    </div>
  );
}
