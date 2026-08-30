import { useMemo } from 'react';
import type { RefObject } from 'react';
import type { SSEStageEvent } from '../../api/types';
import { countContentUnits } from '../../utils/contentCount';
import { agentStageLabel, contentLanguageLabel } from '../../utils/uiText';
import '../../styles/writing-redesign.css';

interface ChapterEditorProps {
  chapter: number;
  title: string;
  goal?: string | null;
  text: string;
  textareaRef: RefObject<HTMLTextAreaElement>;
  dirty: boolean;
  canSave: boolean;
  saving: boolean;
  saveMessage: string;
  generating: boolean;
  awaitingGenerationSettlement?: boolean;
  generationFinished?: boolean;
  generationStages: SSEStageEvent[];
  generationError?: string | null;
  contentLanguage?: string | null;
  /** Stops waiting for SSE only; the page keeps generation locked until the backend run settles. */
  onCancelGeneration?: () => void;
  /** Optional profile target. Keep the stat visible without inventing a target when absent. */
  targetWords?: number | null;
  /** Optional hand-off to the next chapter after a draft is accepted. */
  onContinue?: () => void;
  canContinue?: boolean;
  canEdit?: boolean;
  onChange: (value: string) => void;
  onSave: () => void;
  onGenerate: () => void;
}

const STAGE_LABELS: Record<string, string> = {
  recall: '读取前文与已确认设定',
  plan: '核对本章目标',
  candidates: '准备候选正文',
  draft: '生成正文',
  check: '检查设定冲突',
  plan_alignment: '核对故事路线',
  gate: '检查正文质量',
  plan_reconcile: '更新后续计划',
  settle: '更新已确认设定',
  quality: '评估文本表现',
};

const STAGE_ORDER = Object.keys(STAGE_LABELS);

function stageLabel(stage: string): string {
  const normalized = stage.trim().toLowerCase();
  return STAGE_LABELS[normalized] ?? agentStageLabel(normalized);
}

function stageStatusClass(status: string, active: boolean): string {
  const normalized = status.trim().toLowerCase();
  if (['blocked', 'error', 'failed', 'circuit_broken'].includes(normalized)) return 'blocked';
  if (['ok', 'completed', 'complete', 'done', 'success'].includes(normalized)) return 'done';
  if (['skipped', 'degraded'].includes(normalized)) return 'muted';
  if (active) return 'active';
  return 'pending';
}

function rawNumericDetail(detail: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const raw = detail[key];
    if (typeof raw === 'number' && Number.isFinite(raw)) {
      return raw;
    }
    if (typeof raw === 'string' && raw.trim() !== '') {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

function percentDetail(detail: Record<string, unknown>, keys: string[]): number | null {
  const value = rawNumericDetail(detail, keys);
  if (value == null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function stageProgress(stages: SSEStageEvent[], generating: boolean, finished: boolean): number {
  if (finished) return 100;
  if (stages.length === 0) return generating ? 8 : 0;
  const latest = stages[stages.length - 1];
  const explicit = percentDetail(latest.detail ?? {}, [
    'progress', 'progress_pct', 'progress_percent', 'percentage', 'percent',
  ]);
  if (explicit != null) return clampPercent(explicit);

  const doneCount = stages.filter((stage) => stage.status === 'ok' || stage.status === 'completed').length;
  const inferred = (doneCount / STAGE_ORDER.length) * 100;
  if (generating) return clampPercent(Math.max(8, inferred));
  return clampPercent(inferred);
}

function stageEta(stages: SSEStageEvent[]): string | null {
  if (stages.length === 0) return null;
  const detail = stages[stages.length - 1].detail ?? {};
  const seconds = rawNumericDetail(detail, ['eta_seconds', 'remaining_seconds', 'eta']);
  if (seconds == null || seconds < 0) return null;
  return `${Math.max(1, Math.round(seconds))} 秒后完成`;
}

export default function ChapterEditor({
  chapter,
  title,
  goal,
  text,
  textareaRef,
  dirty,
  canSave,
  saving,
  saveMessage,
  generating,
  awaitingGenerationSettlement = false,
  generationFinished = false,
  generationStages,
  generationError,
  contentLanguage,
  onCancelGeneration,
  targetWords,
  onContinue,
  canContinue = true,
  canEdit = true,
  onChange,
  onSave,
  onGenerate,
}: ChapterEditorProps): JSX.Element {
  const count = countContentUnits(text, contentLanguage);
  const numberLocale = contentLanguage?.startsWith('en') ? 'en-US' : 'zh-CN';
  const countLabel = count.unit === 'words' ? '词数' : '字数';
  const uniqueStages = useMemo(() => {
    const byStage = new Map<string, SSEStageEvent>();
    generationStages.forEach((stage) => byStage.set(stage.stage, stage));
    return Array.from(byStage.values());
  }, [generationStages]);
  const progress = stageProgress(generationStages, generating, generationFinished);
  const activeStage = uniqueStages.length > 0
    ? uniqueStages[uniqueStages.length - 1]
    : null;
  const activeStageLabel = awaitingGenerationSettlement
    ? '已停止等待页面进度，正在核对后台结果'
    : activeStage
      ? stageLabel(activeStage.stage)
      : '准备生成';
  const eta = stageEta(generationStages);
  const stageRows = uniqueStages.length > 0
    ? uniqueStages
    : [{ event: 'stage', stage: 'recall', status: 'pending', detail: {} } satisfies SSEStageEvent];
  const chapterLabel = `第 ${chapter} 章`;

  return (
    <main className="manuscript-desk">
      <header className="manuscript-head">
        <div className="manuscript-number" aria-hidden="true">{String(chapter).padStart(2, '0')}</div>
        <div className="manuscript-title">
          <div className="writing-redesign-kicker">
            <span>第 {String(chapter).padStart(2, '0')} 章 · 正文编辑中</span>
            <span className={`writing-draft-state${dirty ? ' is-dirty' : ''}`}>
              {dirty ? '未保存' : canSave ? '已保存' : '待起稿'}
            </span>
          </div>
          <h1>{title || `第 ${chapter} 章`}</h1>
          {goal && <p>{goal}</p>}
        </div>
        <div className="manuscript-actions">
          {onContinue && (
            <button
              type="button"
              className="desk-button continue"
              onClick={onContinue}
              disabled={!canContinue}
              title={!canContinue ? '请先完成生成、选稿并保存当前正文' : undefined}
            >
              继续写下一章 <span aria-hidden="true">→</span>
            </button>
          )}
          <button
            type="button"
            className="desk-button secondary"
            onClick={onSave}
            disabled={saving || !dirty || !canSave}
          >
            {saving ? '保存中…' : !canSave ? '等待修订' : dirty ? '保存修订' : '已保存'}
          </button>
          <button
            type="button"
            className="desk-button primary"
            onClick={onGenerate}
            disabled={generating || awaitingGenerationSettlement || dirty}
            title={dirty ? '请先保存当前修改再生成候选' : undefined}
          >
            {awaitingGenerationSettlement
              ? '正在确认后台结果'
              : generating
                ? <><span className="nf-spin" /> 正在锻稿</>
                : '✦ 生成候选'}
          </button>
        </div>
      </header>

      <div className="manuscript-meta writing-redesign-meta">
        <div className="writing-stat">
          <span>目标{countLabel}</span>
          <strong>{targetWords == null ? '—' : targetWords.toLocaleString(numberLocale)}</strong>
        </div>
        <div className="writing-stat">
          <span>已写{countLabel}</span>
          <strong>{count.value.toLocaleString(numberLocale)}</strong>
        </div>
        <span className="writing-save-message" role="status" aria-live="polite">{saveMessage}</span>
        <span className="writing-shortcut">⌘ 或 Ctrl + S 保存</span>
      </div>

      <div className="writing-context-strip" aria-label="本章写作上下文">
        <span className="writing-context-label">上下文</span>
        <span>截至第 {Math.max(0, chapter - 1)} 章</span>
        <span>{contentLanguageLabel(contentLanguage)}</span>
        <span>{goal ? '章节目标已锁定' : '章节目标待补充'}</span>
      </div>

      {(generating || awaitingGenerationSettlement || generationStages.length > 0) && (
        <section className="writing-generation-rail" aria-live="polite" aria-label="章节生成进度">
          <div className="writing-generation-head">
            <div className="writing-generation-title">
              <span className="writing-generation-mark" aria-hidden="true">助手</span>
              <div>
                <strong>{awaitingGenerationSettlement ? '正在确认后台结果' : generating ? '创作助手生成中…' : '生成进度'}</strong>
                <span>{activeStageLabel}</span>
              </div>
            </div>
            <div className="writing-generation-actions">
              {eta && generating && !awaitingGenerationSettlement && <span className="writing-generation-eta">{eta}</span>}
              <strong>{Math.round(progress)}%</strong>
              {generating && onCancelGeneration && (
                <button type="button" className="writing-generation-stop" onClick={onCancelGeneration}>
                  停止等待
                </button>
              )}
            </div>
          </div>
          <div
            className="writing-generation-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress)}
            aria-valuetext={`${Math.round(progress)}%，${activeStageLabel}`}
          >
            <span style={{ width: `${progress}%` }} />
          </div>
          <ol className="writing-generation-stages">
            {stageRows.map((stage, index) => {
              const normalizedStatus = stage.status.trim().toLowerCase();
              const active = generating
                && index === stageRows.length - 1
                && !['blocked', 'error', 'failed', 'circuit_broken', 'ok', 'completed', 'complete', 'done', 'success'].includes(normalizedStatus);
              const state = stageStatusClass(stage.status, active);
              return (
                <li className={`writing-generation-stage ${state}`} key={`${stage.stage}-${index}`}>
                  <span className="writing-generation-stage-dot" aria-hidden="true" />
                  <span>{stageLabel(stage.stage)}</span>
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {generationError && <div className="desk-alert" role="alert">{generationError}</div>}

      <div className="manuscript-paper">
        <div className="paper-margin" aria-hidden="true">草稿 · 设定待确认</div>
        <div className="writing-paper-topline" aria-hidden="true">
          <span>{chapterLabel} · 正文</span>
          <span>{dirty ? '有未保存修改' : canSave ? '已保存到服务端' : '先生成候选建立修订'}</span>
        </div>
        <textarea
          ref={textareaRef}
          value={text}
          readOnly={!canEdit}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
              event.preventDefault();
              onSave();
            }
          }}
          aria-label={`第 ${chapter} 章正文`}
          aria-describedby={!canEdit ? 'writing-edit-lock-note' : undefined}
          spellCheck
          placeholder={canEdit
            ? '在这里继续修订正文。'
            : '先使用「生成候选」建立本章修订，再进入可编辑正文。'}
        />
        <div className="writing-paper-footer">
          <span id="writing-edit-lock-note">{!canEdit ? '当前稿纸只读；等待本章修订就绪' : dirty ? '修改尚未保存' : saveMessage}</span>
          <span>{count.value.toLocaleString(numberLocale)} {count.unit === 'words' ? '词' : '字'}</span>
        </div>
      </div>
    </main>
  );
}
