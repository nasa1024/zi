import type { ChapterPlanVersion } from '../../api/types';
import { planReasonLabel, planStatusLabel } from '../../utils/uiText';

interface PlanVersionTimelineProps {
  versions: ChapterPlanVersion[];
}

export default function PlanVersionTimeline({ versions }: PlanVersionTimelineProps): JSX.Element | null {
  if (versions.length === 0) return null;
  const sorted = versions.slice().sort((a, b) => b.version - a.version);

  return (
    <section className="plan-version-panel" aria-labelledby="plan-version-title">
      <div className="inspector-section-head">
        <span>历史存档</span>
        <h3 id="plan-version-title">计划版本</h3>
      </div>
      <ol className="plan-version-list">
        {sorted.map((version) => (
          <li className={version.status} key={version.id}>
            <span className="plan-version-dot" aria-hidden="true" />
            <div>
              <b>第 {version.version} 版 · {planStatusLabel(version.status)}</b>
              <p>{planReasonLabel(version.reason, version.status)}</p>
              {version.divergence_score != null && (
                <small>与原计划偏离 {Math.round(version.divergence_score * 100)}%</small>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
