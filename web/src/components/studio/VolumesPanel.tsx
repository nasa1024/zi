// VolumesPanel（POST-v15 P2#5/P2#6）：卷级 Objective/KR 管理 + 设定→卷向导。
// - 每卷扩展卡片：Objective、KR 列表（met/partial/missed 状态 + evidence）
// - KR 可增删改、手动触发 settle-kr 结算、卷状态流转
// - 「从设定生成首卷」向导：锁定的情节设定 → 建卷 + 预填前几章细纲

import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { KeyResult, VolumeInfo, VolumeWizardResponse } from '../../api/types';
import {
  keyResultStatusLabel,
  userFacingError,
  volumeStatusLabel,
} from '../../utils/uiText';

function errMessage(err: unknown, fallback: string): string {
  return userFacingError(err, fallback);
}

const KR_STATUS_META: Record<string, { icon: string; color: string }> = {
  met: { icon: '✅', color: '#4ade80' },
  partial: { icon: '🟡', color: '#fbbf24' },
  missed: { icon: '🔴', color: '#f87171' },
  pending: { icon: '⏳', color: 'rgba(255,255,255,0.5)' },
};

function KrStatusChip({ kr }: { kr: KeyResult }): JSX.Element {
  const meta = KR_STATUS_META[kr.status] ?? KR_STATUS_META.pending;
  return (
    <span
      className="rs-chip"
      style={{ color: meta.color }}
      title={kr.evidence ? `证据：${kr.evidence}` : '暂无剧情证据'}
    >
      {meta.icon} {keyResultStatusLabel(kr.status)}
    </span>
  );
}

// 单卷扩展卡片：Objective + KR 编辑器
function VolumeCard({
  projectId,
  vol,
  onChanged,
}: {
  projectId: string;
  vol: VolumeInfo;
  onChanged: () => void;
}): JSX.Element {
  const [objective, setObjective] = useState<string>(vol.objective ?? '');
  const [krs, setKrs] = useState<KeyResult[]>(vol.key_results ?? []);
  const [newKr, setNewKr] = useState<string>('');
  const [busy, setBusy] = useState<boolean>(false);
  const [settling, setSettling] = useState<boolean>(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setObjective(vol.objective ?? '');
    setKrs(vol.key_results ?? []);
  }, [vol]);

  const dirty =
    objective !== (vol.objective ?? '') ||
    JSON.stringify(krs.map((k) => k.text)) !==
      JSON.stringify((vol.key_results ?? []).map((k) => k.text));

  const save = async () => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await api.updateVolume(projectId, vol.volume_no, {
        objective: objective.trim() || null,
        // 保留已有 id/status/evidence，文本改动即视为新 KR 判定基准
        key_results: krs,
      });
      setMsg('已保存');
      onChanged();
    } catch (err) {
      setError(errMessage(err, '保存失败'));
    } finally {
      setBusy(false);
    }
  };

  const settle = async () => {
    setSettling(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.settleVolumeKr(projectId, vol.volume_no);
      if (r.settled) {
        setMsg(`结算完成：兑现 ${r.met ?? 0} · 部分 ${r.partial ?? 0} · 未兑现 ${r.missed ?? 0}`);
        onChanged();
      } else {
        setError('尚未满足结算条件，请检查卷目标与关键结果是否完整。');
      }
    } catch (err) {
      setError(errMessage(err, '关键结果结算失败，请检查模型服务设置后重试。'));
    } finally {
      setSettling(false);
    }
  };

  const addKr = () => {
    const text = newKr.trim();
    if (!text) return;
    setKrs((prev) => [
      ...prev,
      { id: `kr${prev.length + 1}_${Date.now()}`, text, status: 'pending', evidence: '' },
    ]);
    setNewKr('');
  };

  const editKr = (id: string, text: string) => {
    setKrs((prev) => prev.map((k) => (k.id === id ? { ...k, text } : k)));
  };

  const removeKr = (id: string) => {
    setKrs((prev) => prev.filter((k) => k.id !== id));
  };

  const markCompleted = async () => {
    setBusy(true);
    setError(null);
    try {
      // status→completed 时后端自动结算 KR（best-effort）
      await api.updateVolume(projectId, vol.volume_no, { status: 'completed' });
      setMsg('卷已完结，关键结果自动结算已触发');
      onChanged();
    } catch (err) {
      setError(errMessage(err, '状态更新失败'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="review-card">
      <div className="rc-top">
        <span className="rc-type">第 {vol.volume_no} 卷 · {vol.title}</span>
        <span className={`risk-tag ${vol.status === 'completed' ? 'low' : 'medium'}`}>
          {volumeStatusLabel(vol.status)}
        </span>
        <span className="rc-spacer" />
        {vol.start_chapter != null && (
          <span className="rc-status">
            第 {vol.start_chapter}–{vol.end_chapter ?? '…'} 章
          </span>
        )}
      </div>

      {vol.synopsis && <div className="ph-hint" style={{ margin: '0.3rem 0' }}>{vol.synopsis}</div>}

      <div className="nf-field full" style={{ marginTop: '0.5rem' }}>
        <label>🎯 本卷目标</label>
        <input
          className="nf-input"
          placeholder="例如：李云探索秘境并在外门立足"
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          disabled={busy}
        />
      </div>

      <div className="nf-field full" style={{ marginTop: '0.4rem' }}>
        <label>📌 关键结果</label>
        {krs.length === 0 && (
          <div className="ph-hint">尚无关键结果 —— 在下方添加可判定的结果</div>
        )}
        {krs.map((kr) => (
          <div
            key={kr.id}
            style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', marginTop: '0.3rem' }}
          >
            <KrStatusChip kr={kr} />
            <input
              className="nf-input"
              style={{ flex: 1 }}
              value={kr.text}
              onChange={(e) => editKr(kr.id, e.target.value)}
              disabled={busy}
            />
            <button
              type="button"
              className="nf-btn-sm"
              onClick={() => removeKr(kr.id)}
              disabled={busy}
              title="删除此关键结果"
            >
              ✕
            </button>
          </div>
        ))}
        <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.4rem' }}>
          <input
            className="nf-input"
            style={{ flex: 1 }}
            placeholder="新的关键结果，例如：获得天道之眼"
            value={newKr}
            onChange={(e) => setNewKr(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addKr(); } }}
            disabled={busy}
          />
          <button type="button" className="nf-btn-sm" onClick={addKr} disabled={busy || !newKr.trim()}>
            ＋ 添加
          </button>
        </div>
      </div>

      {/* KR evidence 明细（有结算结果时展示） */}
      {krs.some((k) => k.evidence) && (
        <div className="ph-hint" style={{ marginTop: '0.4rem' }}>
          {krs.filter((k) => k.evidence).map((k) => (
            <div key={k.id}>📎 {k.text.slice(0, 20)}…：{k.evidence}</div>
          ))}
        </div>
      )}

      <div className="rc-actions" style={{ marginTop: '0.5rem' }}>
        <button type="button" className="nf-btn sm" onClick={() => void save()} disabled={busy || !dirty}>
          {busy ? '保存中…' : '💾 保存卷目标'}
        </button>
        <button
          type="button"
          className="nf-btn blue sm"
          onClick={() => void settle()}
          disabled={settling || !vol.objective || (vol.key_results ?? []).length === 0}
          title="对照本卷章摘要判定每条关键结果的兑现度；完全兑现需要剧情证据"
        >
          {settling ? '结算中…' : '⚖️ 结算关键结果'}
        </button>
        {vol.status === 'writing' && (
          <button
            type="button"
            className="nf-btn pink sm"
            onClick={() => void markCompleted()}
            disabled={busy}
            title="卷完结时自动触发关键结果结算"
          >
            🏁 标记完结
          </button>
        )}
      </div>

      {msg && <div className="nf-msg ok"><span>✓</span><span>{msg}</span></div>}
      {error && <div className="nf-msg err"><span>⚠</span><span>{error}</span></div>}
    </div>
  );
}

export default function VolumesPanel({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged: () => void;
}): JSX.Element {
  const [volumes, setVolumes] = useState<VolumeInfo[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // 新建卷表单
  const [showCreate, setShowCreate] = useState<boolean>(false);
  const [cTitle, setCTitle] = useState<string>('');
  const [cSynopsis, setCSynopsis] = useState<string>('');
  const [cStart, setCStart] = useState<string>('');
  const [cEnd, setCEnd] = useState<string>('');
  const [creating, setCreating] = useState<boolean>(false);

  // 设定→卷向导
  const [wizBusy, setWizBusy] = useState<boolean>(false);
  const [wizResult, setWizResult] = useState<VolumeWizardResponse | null>(null);
  const [wizError, setWizError] = useState<string | null>(null);
  const [wizHints, setWizHints] = useState<string>('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setVolumes(await api.listVolumes(projectId));
    } catch (err) {
      setError(errMessage(err, '加载卷列表失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    if (!cTitle.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const nextNo = volumes.reduce((m, v) => Math.max(m, v.volume_no), 0) + 1;
      const start = Number.parseInt(cStart, 10);
      const end = Number.parseInt(cEnd, 10);
      await api.createVolume(projectId, {
        volume_no: nextNo,
        title: cTitle.trim(),
        synopsis: cSynopsis.trim() || null,
        start_chapter: Number.isFinite(start) ? start : null,
        end_chapter: Number.isFinite(end) ? end : null,
      });
      setCTitle(''); setCSynopsis(''); setCStart(''); setCEnd('');
      setShowCreate(false);
      await load();
      onChanged();
    } catch (err) {
      setError(errMessage(err, '建卷失败'));
    } finally {
      setCreating(false);
    }
  };

  const runWizard = async () => {
    setWizBusy(true);
    setWizError(null);
    setWizResult(null);
    try {
      const r = await api.volumeWizard(projectId, {
        volume_span: 20, plan_chapters: 5,
        hints: wizHints.trim() || null,
      });
      setWizResult(r);
      await load();
      onChanged();
    } catch (err) {
      setWizError(errMessage(err, '向导失败——项目可能没有锁定的情节设定'));
    } finally {
      setWizBusy(false);
    }
  };

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">卷目标</h3>
        <span className="ph-hint">设定卷目标与可验证结果，卷末结算兑现度</span>
        <button type="button" className="nf-btn ghost sm" onClick={() => void load()} disabled={loading}>
          ↻ 刷新
        </button>
      </div>

      {/* ── 设定→卷向导 ─────────────────────────────── */}
      <div className="panel-section-head">
        <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>🪄 设定 → 卷大纲向导</span>
      </div>
      <div className="ph-hint">
        读取建项目时锁定的情节设定（主线冲突/力量阶梯/主角），一键生成下一卷的
        大纲与关键结果，并预填前 5 章的章节安排与故事节拍——设定即细纲。
      </div>
      <div className="nf-actions" style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', flexWrap: 'wrap', marginTop: '0.5rem' }}>
        <input
          className="nf-input"
          style={{ flex: 1, minWidth: '12rem' }}
          placeholder="方向提示（可选），例如：本卷以宗门大比收尾"
          value={wizHints}
          onChange={(e) => setWizHints(e.target.value)}
          disabled={wizBusy}
        />
        <button type="button" className="nf-btn" onClick={() => void runWizard()} disabled={wizBusy}>
          {wizBusy ? (<><span className="nf-spin" /> 生成中…</>) : (<>🪄 生成下一卷</>)}
        </button>
      </div>
      {wizError && <div className="nf-msg err"><span>⚠</span><span>{wizError}</span></div>}
      {wizResult && (
        <div className="nf-msg ok">
          <span>✓</span>
          <span>
            已创建第 {wizResult.volume.volume_no} 卷《{wizResult.volume.title}》
            {wizResult.plan && wizResult.plan.planned.length > 0 && (
              <>，预填 {wizResult.plan.planned.length} 章细纲（去「⚙️ 生成章节」查看）</>
            )}
            {wizResult.degraded && <>。⚠ 未配置模型服务，已使用确定性兜底大纲；配置后可重试</>}
            {wizResult.tier_used && <>（已使用配置的模型档位）</>}
          </span>
        </div>
      )}

      {/* ── 卷列表 ─────────────────────────────────── */}
      <div className="panel-section-head" style={{ marginTop: '1rem' }}>
        <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>📚 卷列表</span>
        <button
          type="button"
          className="nf-btn-sm"
          style={{ marginLeft: 'auto' }}
          onClick={() => setShowCreate((v) => !v)}
        >
          {showCreate ? '收起' : '＋ 手动建卷'}
        </button>
      </div>

      {showCreate && (
        <div className="nf-form" style={{ marginTop: '0.5rem' }}>
          <div className="nf-field">
            <label>卷名<span className="req">*</span></label>
            <input className="nf-input" value={cTitle} onChange={(e) => setCTitle(e.target.value)} disabled={creating} />
          </div>
          <div className="nf-field">
            <label>起始章</label>
            <input className="nf-input" type="number" min={1} value={cStart} onChange={(e) => setCStart(e.target.value)} disabled={creating} />
          </div>
          <div className="nf-field">
            <label>末章</label>
            <input className="nf-input" type="number" min={1} value={cEnd} onChange={(e) => setCEnd(e.target.value)} disabled={creating} />
          </div>
          <div className="nf-field full">
            <label>卷大纲</label>
            <textarea className="nf-textarea" rows={2} value={cSynopsis} onChange={(e) => setCSynopsis(e.target.value)} disabled={creating} />
          </div>
          <div className="nf-actions" style={{ gridColumn: '1 / -1' }}>
            <button type="button" className="nf-btn" onClick={() => void create()} disabled={creating || !cTitle.trim()}>
              {creating ? '创建中…' : '⚡ 创建卷'}
            </button>
          </div>
        </div>
      )}

      {loading && <span className="nf-loading"><span className="nf-spin" /> 加载中…</span>}
      {error && <div className="nf-msg err"><span>⚠</span><span>{error}</span></div>}
      {!loading && volumes.length === 0 && (
        <div className="nf-hollow">尚无卷 —— 用上方向导从设定一键生成，或手动建卷。</div>
      )}

      {volumes.length > 0 && (
        <div className="review-list" style={{ marginTop: '0.6rem' }}>
          {volumes.map((v) => (
            <VolumeCard key={v.id} projectId={projectId} vol={v} onChanged={() => { void load(); onChanged(); }} />
          ))}
        </div>
      )}
    </div>
  );
}
