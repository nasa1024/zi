import AgentPanel from '../../components/studio/AgentPanel';
import VolumesPanel from '../../components/studio/VolumesPanel';
import PlanWindowBoard from '../../components/planning/PlanWindowBoard';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';
import '../../styles/plan-redesign.css';

export default function PlanWorkspacePage(): JSX.Element {
  const { project, refetchProjects } = useWorkspaceOutlet();
  const nextChapter = Math.max(1, project.chapter_count + 1);

  return (
    <main className="routed-workspace-page plan-route-page">
      <header className="route-page-head">
        <div>
          <span>当前创作环节 · 定方向</span>
          <h1>规划室</h1>
        </div>
        <p>先把方向选得顺手，再只排眼前几章。每次选择都会留下创作记录，已经发生的正文不会被倒写。</p>
      </header>

      <section className="plan-next-strip" aria-label="当前创作位置">
        <div className="plan-next-index" aria-hidden="true">当前</div>
        <div>
          <span>你现在要决定的是</span>
          <strong>第 {nextChapter} 章往哪里走</strong>
          <p>{project.chapter_count > 0 ? `前 ${project.chapter_count} 章已经落盘，接下来只需要给下一章一个可执行的动作。` : '还没有正文；先选一条全书路线，zi 会把第一章拆成可写的动作。'}</p>
        </div>
        <a href="#future" className="plan-next-link">跳到未来窗口 ↓</a>
      </section>

      <section className="plan-stage plan-direction-stage" aria-labelledby="plan-direction-title">
        <header className="plan-stage-head">
          <div><span>当前 · 定方向</span><h2 id="plan-direction-title">先选一条你愿意写下去的路线</h2></div>
          <p>不用一次填完整大纲。看见差异、做一个选择，创作助手会继续把路线拆开。</p>
        </header>
        <AgentPanel
          projectId={project.project_id}
          contentLanguage={project.content_language}
          onChanged={refetchProjects}
        />
      </section>

      <section className="plan-stage plan-future-stage" id="future" aria-labelledby="plan-future-title">
        <header className="plan-stage-head">
          <div><span>下一步 · 排未来</span><h2 id="plan-future-title">只排接下来几章，不把自己困在大纲里</h2></div>
          <p>给窗口一个提示，生成三条路线；采用后会变成下一章可检查的契约。</p>
        </header>
        <PlanWindowBoard
          projectId={project.project_id}
          defaultStartChapter={nextChapter}
          onChanged={refetchProjects}
        />
      </section>

      <section className="plan-stage plan-volume-stage" aria-labelledby="plan-volume-title">
        <header className="plan-stage-head">
          <div><span>可选 · 卷目标</span><h2 id="plan-volume-title">需要时，再给这一卷一个方向</h2></div>
          <p>卷目标和关键结果是护栏，不是额外表格；可以先写，之后再补。</p>
        </header>
        <VolumesPanel projectId={project.project_id} onChanged={refetchProjects} />
      </section>
    </main>
  );
}
