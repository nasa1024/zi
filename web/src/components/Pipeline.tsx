export function Pipeline() {
  return (
    <section className="section pipeline" id="pipeline">
      <div className="wrap">
        <div className="sec-head reveal">
          <span className="eyebrow">写作流水线</span>
          <h2>一条流水线，<span className="hi-pink">五道工序</span></h2>
          <p>从规划到保存，每一章都走同一套稳定流程。最后由你选择哪些新设定可以成为已确认设定。</p>
        </div>
        <div className="flow">
          <div className="step reveal"><div className="sn">第 1 步</div><h4>规划</h4><div className="zh cn">定方向</div><p>确定本章目标、关键节拍、价值变化与章末钩子。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">第 2 步</div><h4>召回</h4><div className="zh cn">找设定</div><p>优先找回相关的已确认设定与历史片段。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">第 3 步</div><h4>起草</h4><div className="zh cn">写初稿</div><p>按当前章节注入约束，再由写作模型生成正文初稿。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">第 4 步</div><h4>校验</h4><div className="zh cn">查问题</div><p>规则校验与写作质量检查共同过滤明显问题。</p><span className="arrow">→</span></div>
          <div className="step reveal"><div className="sn">第 5 步</div><h4>确认</h4><div className="zh cn">定设定</div><p>新设定会成为已确认设定，或进入审核队列等待你决定。</p></div>
        </div>
        <p className="gate-note reveal">生成中断也不怕：<b>每一步都会保存进度</b>，下次打开时会自动恢复到最近一次完整状态。</p>
      </div>
    </section>
  );
}
