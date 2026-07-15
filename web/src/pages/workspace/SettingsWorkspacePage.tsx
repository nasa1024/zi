import MemoryPanel from '../../components/studio/MemoryPanel';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';

export default function SettingsWorkspacePage(): JSX.Element {
  const { project } = useWorkspaceOutlet();
  return (
    <main className="routed-workspace-page settings-route-page">
      <header className="route-page-head">
        <div><span>ENGINE ROOM · PROJECT SCOPE</span><h1>项目设置</h1></div>
        <p>只保存引擎和界面偏好；项目、章节与会话位置始终由 URL 和后端状态决定。</p>
      </header>
      <MemoryPanel projectId={project.project_id} />
    </main>
  );
}
