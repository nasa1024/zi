import type { ChapterContract, ContractItem } from '../../api/types';
import {
  beatTypeLabel,
  systemCopyText,
  targetStateKeyLabel,
  targetStateValueLabel,
} from '../../utils/uiText';

interface ContractInspectorProps {
  contract: ChapterContract | null;
  loading: boolean;
}

function itemText(item: ContractItem): string {
  if (typeof item === 'string') return systemCopyText(item, '未命名契约项');
  return systemCopyText(item.text || item.label, '未命名契约项');
}

function targetStateLabel(key: string): string {
  return /[\u3400-\u9fff]/u.test(key) ? key : targetStateKeyLabel(key);
}

function targetStateValue(value: unknown): string {
  if (value == null || value === '') return '未设置';
  if (typeof value === 'string') return targetStateValueLabel(value);
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map(targetStateValue).join('、');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => `${targetStateLabel(key)}：${targetStateValue(nested)}`)
      .join('；');
  }
  return '已记录';
}

function ContractList({
  title,
  items,
  tone,
}: {
  title: string;
  items: ContractItem[];
  tone: 'must' | 'forbid' | 'success';
}): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <section className={`contract-block ${tone}`}>
      <h4>{title}</h4>
      <ul>
        {items.map((item, index) => {
          const evidence = typeof item === 'string' ? null : item.evidence;
          const status = typeof item === 'string' ? 'pending' : item.status ?? 'pending';
          return (
            <li className={status} key={`${itemText(item)}-${index}`}>
              <span aria-hidden="true" />
              <div>
                {itemText(item)}
                {evidence && <small>证据：{evidence}</small>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export default function ContractInspector({ contract, loading }: ContractInspectorProps): JSX.Element {
  if (loading) {
    return <div className="inspector-loading" aria-live="polite"><span className="nf-spin" /> 正在展开章节契约…</div>;
  }

  if (!contract) {
    return (
      <section className="contract-empty">
        <span>暂无契约</span>
        <h3>本章还没有锁定路线</h3>
        <p>先到「规划」生成未来窗口；仍可直接写作，正文不会被空契约阻塞。</p>
      </section>
    );
  }

  const mustAdvance = contract.must_advance ?? [];
  const mustResolve = contract.must_resolve ?? [];
  const mustInclude = contract.must_include_entities ?? [];
  const forbids = [
    ...(contract.must_not_reveal ?? []),
    ...(contract.must_not_resolve ?? []),
    ...(contract.forbidden_state_changes ?? []),
  ];

  return (
    <>
      <section className="contract-overview">
        <div className="contract-stamp">
          <span>{contract.locked ? '已锁定' : '可修改'}</span>
          <b>第 {contract.plan_version ?? 1} 版</b>
        </div>
        <div>
          <span>本章写作约定</span>
          <h2>章节契约</h2>
          <p>{contract.goal || '等待定义本章在全书中的作用'}</p>
        </div>
      </section>

      {(contract.premise || contract.ending_direction) && (
        <section className="story-anchor-card">
          <span>全书不变锚点</span>
          {contract.premise && <p><b>核心：</b>{contract.premise}</p>}
          {contract.ending_direction && <p><b>终局：</b>{contract.ending_direction}</p>}
        </section>
      )}

      {contract.volume && (
        <section className="volume-contract-card">
          <span>第 {contract.volume.volume_no ?? '—'} 卷</span>
          <h3>{contract.volume.title || '当前卷'}</h3>
          <p>{contract.volume.objective || '本卷目标尚未填写'}</p>
          {(contract.volume.key_results ?? []).length > 0 && (
            <div className="contract-chips">
              {(contract.volume.key_results ?? []).map((kr) => <span key={kr.id}>{kr.text}</span>)}
            </div>
          )}
        </section>
      )}

      <ContractList title="本章必须推进" items={mustAdvance} tone="must" />
      <ContractList title="本章必须解决" items={mustResolve} tone="must" />
      <ContractList title="必须在场" items={mustInclude} tone="must" />
      <ContractList title="禁止提前发生" items={forbids} tone="forbid" />
      <ContractList title="验收条件" items={contract.success_criteria ?? []} tone="success" />

      {(contract.arc_targets ?? []).length > 0 && (
        <section className="contract-targets">
          <h4>服务的情节弧</h4>
          <div className="contract-chips blue">
            {contract.arc_targets.map((item, index) => <span key={`${itemText(item)}-${index}`}>{itemText(item)}</span>)}
          </div>
        </section>
      )}

      {contract.beats?.length > 0 && (
        <section className="contract-beats">
          <h4>节拍校样</h4>
          <ol>
            {contract.beats.map((beat) => (
              <li className={beat.constraint_level === 'hard' ? 'hard' : ''} key={`${beat.seq}-${beat.summary}`}>
                <span>{beat.seq}</span>
                <div><b>{beatTypeLabel(beat.beat_type)}</b><p>{beat.summary}</p></div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {Object.keys(contract.target_state ?? {}).length > 0 && (
        <section className="target-state-card">
          <h4>写完后应达到</h4>
          {Object.entries(contract.target_state).map(([key, value]) => (
            <p key={key}><b>{targetStateLabel(key)}</b><span>{targetStateValue(value)}</span></p>
          ))}
        </section>
      )}
    </>
  );
}
