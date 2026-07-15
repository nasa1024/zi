import { Navigate, Route, Routes } from 'react-router-dom';
import LandingPage from './pages/LandingPage';
import StudioPage from './pages/StudioPage';
import StudioWorkspacePage from './pages/StudioWorkspacePage';
import PlanWorkspacePage from './pages/workspace/PlanWorkspacePage';
import ReviewWorkspacePage from './pages/workspace/ReviewWorkspacePage';
import SettingsWorkspacePage from './pages/workspace/SettingsWorkspacePage';
import WorldWorkspacePage from './pages/workspace/WorldWorkspacePage';
import WritingWorkspacePage from './pages/workspace/WritingWorkspacePage';

export default function App(): JSX.Element {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="/studio" element={<StudioPage />} />
      <Route path="/studio/:projectId" element={<StudioWorkspacePage />}>
        <Route index element={<Navigate to="plan" replace />} />
        <Route path="write/:chapter" element={<WritingWorkspacePage />} />
        <Route path="plan" element={<PlanWorkspacePage />} />
        <Route path="world" element={<WorldWorkspacePage />} />
        <Route path="review" element={<ReviewWorkspacePage />} />
        <Route path="settings" element={<SettingsWorkspacePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
