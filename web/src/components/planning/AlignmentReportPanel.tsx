import type { AlignmentReport, DivergenceLevel } from '../../api/types';
import { alignmentEvidenceLabel, divergenceLevelLabel, planActionLabel } from '../../utils/uiText';

interface AlignmentReportPanelProps {
  report: AlignmentReport | null;
  loading: boolean;
  onRevise: () => void;
  onReplan: () => void;
  onKeep: () => void;
}

function inferLevel(score: number): DivergenceLevel {
  if (score < 0.15) return 'normal';
  if (score < 0.3) return 'minor';
  if (score <= 0.5) return 'major';
  return 'severe';
}

function percent(value: number): string {
  return `${Math.round((value <= 1 ? value : value / 100) * 100)}%`;
}

export default function AlignmentReportPanel({
  report,
  loading,
  onRevise,
  onReplan,
  onKeep,
}: AlignmentReportPanelProps): JSX.Element {
  if (loading) {
    return <div className="inspector-loading"><span className="nf-spin" /> 正在核对计划与实际…</div>;
  }

  if (!report) {
    return (
      <section className="alignment-empty">
        <span>计划 ↔ 实际</span>
        <h3>等待正文验收</h3>
        <p>生成或保存正文后，这里会显示契约完成度、偏航证据和下一步建议。</p>
      </section>
    );
  }

  const level = report.level && ['normal', 'minor', 'major', 'severe'].includes(report.level)
    ? report.level as DivergenceLevel
    : inferLevel(report.divergence_score);
  const score = Math.max(0, Math.min(1, report.divergence_score));
  const metrics = [
    ['本章目标', report.goal_score],
    ['硬节拍', report.beat_score],
    ['情节弧', report.arc_score],
    ['卷目标', report.volume_score],
    ['揭示护栏', report.reveal_score],
  ] as const;

  return (
    <section className={`alignment-report ${level}`} aria-labelledby="alignment-title">
      <div className="alignment-scoreboard">
        <div className="alignment-score">
          <span>偏航</span>
          <b>{percent(score)}</b>
        </div>
        <div>
          <span>计划与实际对照</span>
          <h2 id="alignment-title">{divergenceLevelLabel(level)}</h2>
          <p>建议动作：{planActionLabel(report.decision ?? report.recommended_plan_action ?? 'keep')}</p>
        </div>
      </div>

      <div className="divergence-ruler" aria-label={`偏航程度 ${percent(score)}`}>
        <span style={{ width: `${score * 100}%` }} />
        <i className="ruler-a">15</i><i className="ruler-b">30</i><i className="ruler-c">50</i>
      </div>

      <dl className="alignment-metrics">
        {metrics.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{percent(value)}</dd>
          </div>
        ))}
      </dl>

      {report.hard_reveal && (
        <div className="alignment-hard-block" role="alert">
          <b>硬阻断</b> 正文触碰了禁止提前揭示项，自动模式应暂停。
        </div>
      )}

      {(report.evidence_spans ?? []).length > 0 && (
        <div className="alignment-evidence">
          <h3>原文证据</h3>
          {(report.evidence_spans ?? []).slice(0, 4).map((evidence, index) => (
            <blockquote key={`${evidence.start ?? index}-${evidence.quote ?? ''}`}>
              {evidence.label && <b>{alignmentEvidenceLabel(evidence.label)}</b>}
              <p>{evidence.quote || '证据位置已记录'}</p>
              {evidence.confidence != null && <small>置信度 {percent(evidence.confidence)}</small>}
            </blockquote>
          ))}
        </div>
      )}

      <div className="alignment-actions">
        <button type="button" onClick={onRevise}>修正文稿</button>
        <button type="button" className="primary" onClick={onReplan}>接受并更新后续</button>
        <button type="button" onClick={onKeep}>保持原计划</button>
      </div>
    </section>
  );
}
