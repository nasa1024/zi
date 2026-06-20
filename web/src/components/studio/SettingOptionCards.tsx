// SettingOptionCards — 情节设定候选卡片网格（只读 + 挑选）。
// 用于「生成」模式：展示 N 个候选，点击选中其一进入可编辑草稿。

import type { PlotSetting } from '../../api/types';

export interface SettingOptionCardsProps {
  candidates: PlotSetting[];
  selectedIdx: number | null;
  onPick: (i: number) => void;
}

const CARD_FIELDS: { key: keyof PlotSetting; label: string }[] = [
  { key: 'premise', label: '一句话设定' },
  { key: 'world_setting', label: '世界观' },
  { key: 'power_system', label: '力量体系' },
  { key: 'protagonist', label: '主角' },
  { key: 'main_conflict', label: '主线冲突' },
  { key: 'opening_hook', label: '开篇钩子' },
];

export default function SettingOptionCards({
  candidates,
  selectedIdx,
  onPick,
}: SettingOptionCardsProps): JSX.Element {
  return (
    <div className="setting-cards">
      {candidates.map((c, i) => (
        <button
          type="button"
          key={i}
          className={`setting-card${selectedIdx === i ? ' selected' : ''}`}
          aria-pressed={selectedIdx === i}
          onClick={() => onPick(i)}
        >
          <span className="sc-badge">候选 {i + 1}</span>
          {CARD_FIELDS.map(({ key, label }) => {
            const v = c[key];
            if (!v) return null;
            return (
              <div className="sc-row" key={String(key)}>
                <span className="sc-k">{label}</span>
                <span className="sc-v">{String(v)}</span>
              </div>
            );
          })}
          {c.power_ranks && c.power_ranks.length > 0 && (
            <div className="sc-row">
              <span className="sc-k">力量阶梯</span>
              <span className="sc-v">{c.power_ranks.join(' → ')}</span>
            </div>
          )}
        </button>
      ))}
    </div>
  );
}
