import AgentPanel from '../../components/studio/AgentPanel';
import VolumesPanel from '../../components/studio/VolumesPanel';
import PlanWindowBoard from '../../components/planning/PlanWindowBoard';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';

export default function PlanWorkspacePage(): JSX.Element {
  const { project, refetchProjects } = useWorkspaceOutlet();

  return (
    <main className="routed-workspace-page plan-route-page">
      <header className="route-page-head">
        <div>
          <span>AUTHOR INTENT · NOT CANON</span>
          <h1>规划室</h1>
        </div>
        <p>从全书锚点到滚动章节契约。计划可以修订，已经发生的正文不会被倒写。</p>
      </header>
      <PlanWindowBoard projectId={project.project_id} onChanged={refetchProjects} />
      <div className="route-page-stack">
        <AgentPanel projectId={project.project_id} onChanged={refetchProjects} />
        <VolumesPanel projectId={project.project_id} onChanged={refetchProjects} />
      </div>
    </main>
  );
}
