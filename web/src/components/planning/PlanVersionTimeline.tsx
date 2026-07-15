import type { ChapterPlanVersion } from '../../api/types';

interface PlanVersionTimelineProps {
  versions: ChapterPlanVersion[];
}

export default function PlanVersionTimeline({ versions }: PlanVersionTimelineProps): JSX.Element | null {
  if (versions.length === 0) return null;
  const sorted = versions.slice().sort((a, b) => b.version - a.version);

  return (
    <section className="plan-version-panel" aria-labelledby="plan-version-title">
      <div className="inspector-section-head">
        <span>ARCHIVE</span>
        <h3 id="plan-version-title">计划版本</h3>
      </div>
      <ol className="plan-version-list">
        {sorted.map((version) => (
          <li className={version.status} key={version.id}>
            <span className="plan-version-dot" aria-hidden="true" />
            <div>
              <b>v{version.version} · {version.status}</b>
              <p>{version.reason || (version.status === 'active' ? '当前执行版本' : '历史路线已保留')}</p>
              {version.divergence_score != null && (
                <small>偏航来源 {Math.round(version.divergence_score * 100)}%</small>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
