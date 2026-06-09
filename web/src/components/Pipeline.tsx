export function Pipeline() {
  return (
    <section className="section pipeline" id="pipeline">
      <div className="wrap">
        <div className="sec-head reveal">
          <span className="eyebrow">THE PIPELINE</span>
          <h2>一条流水线，<span className="hi-pink">五道工序</span></h2>
          <p>从规划到入库，每一章都跑同一条确定性管线。唯一的「晋升闸门」决定双模式如何分叉。</p>
        </div>
        <div className="flow">
          <div className="step reveal"><div className="sn">STAGE 01</div><h4>plan</h4><div className="zh cn">规划</div><p>定本章目标、beats、价值轴与钩子。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">STAGE 02</div><h4>recall</h4><div className="zh cn">召回</div><p>实体优先捞回相关 canon 与历史片段。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">STAGE 03</div><h4>draft</h4><div className="zh cn">起草</div><p>as-of 注入约束，LLM 写出正文初稿。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">STAGE 04</div><h4>check</h4><div className="zh cn">校验</div><p>确定性 validator + 工艺检查双重过滤。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">STAGE 05</div><h4>gate</h4><div className="zh cn">闸门</div><p>晋升 canon 或入审核队列——双模式在此分叉。</p></div>
        </div>
        <p className="gate-note reveal">⚙️ 崩溃也不怕：<b>L0 原子写入</b>（temp→fsync→rename）+ <b>pipeline_run 状态机</b>，下次启动自动扫描残留、回滚到一致态。</p>
      </div>
    </section>
  );
}
