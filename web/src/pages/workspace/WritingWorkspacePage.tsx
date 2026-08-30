import { useCallback, useEffect, useRef, useState } from 'react';
import {
  useBeforeUnload,
  useBlocker,
  useLocation,
  useNavigate,
  useParams,
} from 'react-router-dom';
import type { BlockerFunction } from 'react-router-dom';
import { ApiError, api } from '../../api/client';
import type {
  AlignmentReport,
  ChapterCard,
  ChapterContract,
  ChapterDraft,
  ChapterPlanVersion,
  PipelineRunDetail,
  SSEStageEvent,
  StoryMap,
  StoryMapChapter,
  VolumeInfo,
} from '../../api/types';
import CandidateProofs from '../../components/editor/CandidateProofs';
import ChapterEditor from '../../components/editor/ChapterEditor';
import AlignmentReportPanel from '../../components/planning/AlignmentReportPanel';
import ContractInspector from '../../components/planning/ContractInspector';
import PlanVersionTimeline from '../../components/planning/PlanVersionTimeline';
import ReplanDialog from '../../components/planning/ReplanDialog';
import StorySpine from '../../components/workspace/StorySpine';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';
import { sessionStatusLabel, userFacingError } from '../../utils/uiText';

const EMPTY_MAP: StoryMap = { plan: null, arcs: [], volumes: [], chapters: [] };

async function optional<T>(promise: Promise<T>): Promise<T | null> {
  try {
    return await promise;
  } catch (error) {
    if (error instanceof ApiError && [404, 405, 501].includes(error.status)) return null;
    throw error;
  }
}

function errorMessage(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => { window.setTimeout(resolve, milliseconds); });
}

function isTerminalRunStatus(status: string): boolean {
  return ['completed', 'crashed', 'failed', 'blocked', 'cancelled', 'canceled'].includes(status.toLowerCase());
}

type GenerationReconcileSource = 'stopped' | 'recovered';
type GenerationReconcile = (
  token: number,
  baselineRunIds: Set<string>,
  source: GenerationReconcileSource,
  targetRunId?: string,
) => Promise<void>;

function currentVolume(volumes: VolumeInfo[], chapter: number): VolumeInfo | null {
  return volumes.find((volume) => (
    volume.start_chapter != null
    && volume.start_chapter <= chapter
    && (volume.end_chapter == null || volume.end_chapter >= chapter)
  )) ?? null;
}

function fallbackContract(
  chapter: number,
  card: ChapterCard | undefined,
  map: StoryMap,
): ChapterContract | null {
  if (!card) return null;
  const volume = currentVolume(map.volumes, chapter);
  return {
    chapter,
    title: card.title,
    goal: card.goal,
    plan_version: card.plan_version ?? 1,
    locked: card.locked ?? false,
    premise: map.plan?.premise,
    ending_direction: map.plan?.ending_direction,
    volume: volume ? {
      volume_no: volume.volume_no,
      title: volume.title,
      objective: volume.objective,
      key_results: volume.key_results,
    } : null,
    arc_targets: map.arcs
      .filter((arc) => arc.start_chapter <= chapter && (arc.end_chapter == null || arc.end_chapter >= chapter))
      .map((arc) => ({ id: arc.id, text: arc.title })),
    volume_kr_targets: [],
    milestone_targets: [],
    must_advance: [],
    must_resolve: [],
    must_include_entities: [],
    must_not_reveal: [],
    must_not_resolve: [],
    forbidden_state_changes: [],
    target_state: {},
    success_criteria: [],
    target_emotion: card.target_emotion,
    opening_hook_type: card.opening_hook_type,
    hook_type: card.hook_type,
    expectation_score: card.expectation_score,
    beats: card.beats,
  };
}

function mapCards(cards: ChapterCard[]): StoryMapChapter[] {
  return cards.map((card) => ({
    chapter: card.chapter,
    title: card.title,
    goal: card.goal,
    hook_text: card.hook_text,
    status: card.status,
    plan_version: card.plan_version ?? 0,
    locked: card.locked ?? false,
    source_decision_id: card.source_decision_id,
  }));
}

export default function WritingWorkspacePage(): JSX.Element {
  const { project } = useWorkspaceOutlet();
  const { chapter: chapterParam = '1' } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const chapter = Math.max(1, Number.parseInt(chapterParam, 10) || 1);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const generationAbortRef = useRef<AbortController | null>(null);
  const knownRunIdsRef = useRef<Set<string>>(new Set());
  const generationBaselineRunIdsRef = useRef<Set<string>>(new Set());
  const generationReconcileTokenRef = useRef(0);
  const generationReconcileRef = useRef<GenerationReconcile | null>(null);

  const [storyMap, setStoryMap] = useState<StoryMap>(EMPTY_MAP);
  const [cards, setCards] = useState<ChapterCard[]>([]);
  const [contract, setContract] = useState<ChapterContract | null>(null);
  const [alignment, setAlignment] = useState<AlignmentReport | null>(null);
  const [versions, setVersions] = useState<ChapterPlanVersion[]>([]);
  const [draft, setDraft] = useState<ChapterDraft | null>(null);
  const [text, setText] = useState('');
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState('正文已与服务端同步');
  const [generating, setGenerating] = useState(false);
  const [awaitingGenerationSettlement, setAwaitingGenerationSettlement] = useState(false);
  const [generationFinished, setGenerationFinished] = useState(false);
  const [generationStages, setGenerationStages] = useState<SSEStageEvent[]>([]);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<PipelineRunDetail | null>(null);
  const [candidatesHistorical, setCandidatesHistorical] = useState(false);
  const [selecting, setSelecting] = useState<number | null>(null);
  const [inspectorTab, setInspectorTab] = useState<'contract' | 'alignment'>('contract');
  const [replanOpen, setReplanOpen] = useState(false);

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    setError(null);
    setRunDetail(null);
    try {
      const [map, chapterCards, serverDraft, serverContract, report, planVersions, runs] = await Promise.all([
        api.storyMap(project.project_id),
        api.listChapterCards(project.project_id, 1, 9999),
        optional(api.getChapterDraft(project.project_id, chapter)),
        optional(api.getChapterContract(project.project_id, chapter)),
        optional(api.getChapterAlignment(project.project_id, chapter)),
        optional(api.getChapterPlanVersions(project.project_id, chapter)),
        api.listPipelineRuns(project.project_id, 100),
      ]);
      knownRunIdsRef.current = new Set(runs.map((run) => run.run_id));

      const activeRun = runs.find((run) => (
        run.chapter === chapter && !isTerminalRunStatus(run.status)
      ));
      // A persisted non-terminal run means a previous page/SSE session ended
      // before the backend did. Recover fail-closed and follow that exact run;
      // the shared token ensures this replaces, rather than duplicates, any
      // older stop/recovery poller.
      if (activeRun && generationAbortRef.current == null) {
        const token = generationReconcileTokenRef.current + 1;
        generationReconcileTokenRef.current = token;
        const baselineRunIds = new Set(runs.map((run) => run.run_id));
        baselineRunIds.delete(activeRun.run_id);
        setAwaitingGenerationSettlement(true);
        setGenerationFinished(false);
        setSaveMessage('已恢复后台生成对账；本章暂不可重复运行');
        setGenerationError('检测到刷新前尚未结束的后台任务，正在持续核对结果。');
        void generationReconcileRef.current?.(
          token,
          baselineRunIds,
          'recovered',
          activeRun.run_id,
        );
      }

      let recoveredDraft = serverDraft;
      const latestRun = runs.find((run) => run.chapter === chapter && run.status === 'completed');
      if (latestRun) {
        const detail = await api.getPipelineRun(project.project_id, latestRun.run_id);
        if (!recoveredDraft?.draft_text && detail.draft_text) {
          recoveredDraft = {
            chapter,
            draft_text: detail.draft_text,
            word_count: detail.word_count,
          };
        }
        setRunDetail(detail);
        setCandidatesHistorical(Boolean(
          recoveredDraft?.draft_text
          && detail.draft_text
          && recoveredDraft.draft_text !== detail.draft_text
        ));
      } else {
        setCandidatesHistorical(false);
      }

      const mapWithChapters = map.chapters.length > 0 ? map : { ...map, chapters: mapCards(chapterCards) };
      const card = chapterCards.find((item) => item.chapter === chapter);
      setStoryMap(mapWithChapters);
      setCards(chapterCards);
      setDraft(recoveredDraft);
      setText(recoveredDraft?.draft_text ?? '');
      setContract(serverContract ?? fallbackContract(chapter, card, mapWithChapters));
      setAlignment(report);
      setVersions(planVersions ?? []);
      setDirty(false);
      setSaveMessage(
        activeRun
          ? '已恢复后台生成对账；本章暂不可重复运行'
          : recoveredDraft?.revision_id
            ? '正文已与服务端同步'
            : recoveredDraft?.draft_text
              ? '已恢复旧稿；生成候选后会建立可编辑修订'
              : '尚无正文修订；先生成候选再编辑保存',
      );
      return true;
    } catch (loadError) {
      setError(errorMessage(loadError, '写作工作区加载失败'));
      return false;
    } finally {
      setLoading(false);
    }
  }, [chapter, project.project_id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (location.hash === '#proofread' || location.hash === '#continue') {
      setInspectorTab('alignment');
    }
  }, [location.hash]);

  useEffect(() => () => {
    generationReconcileTokenRef.current += 1;
    generationAbortRef.current?.abort();
  }, []);

  const syncLatestDraft = useCallback(async (fallbackText = ''): Promise<ChapterDraft> => {
    const latest = await api.getChapterDraft(project.project_id, chapter);
    setDraft(latest);
    setText(latest.draft_text || fallbackText);
    setDirty(false);
    setSaveMessage(
      latest.revision_id
        ? `已同步修订 ${latest.revision_round ?? ''}`.trim()
        : '正文已落盘，但服务端尚未返回可编辑修订版本',
    );
    return latest;
  }, [chapter, project.project_id]);

  const revisionId = draft?.revision_id ?? null;
  const workspaceSearch = new URLSearchParams(location.search).get('session') ? location.search : '';
  const navigationUnsafe = dirty || generating || awaitingGenerationSettlement || selecting != null;
  const shouldBlockNavigation = useCallback<BlockerFunction>(({ currentLocation, nextLocation }) => {
    const sameDocument = currentLocation.pathname === nextLocation.pathname
      && currentLocation.search === nextLocation.search;
    return navigationUnsafe && !sameDocument;
  }, [navigationUnsafe]);
  const blocker = useBlocker(shouldBlockNavigation);

  useBeforeUnload(useCallback((event) => {
    if (!navigationUnsafe) return;
    event.preventDefault();
    event.returnValue = '';
  }, [navigationUnsafe]));

  useEffect(() => {
    if (blocker.state !== 'blocked') return undefined;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') blocker.reset();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [blocker]);

  const navigateToStage = useCallback((hash: '' | '#candidates' | '#proofread' | '#continue'): void => {
    navigate({ pathname: location.pathname, search: location.search, hash }, { replace: true });
  }, [location.pathname, location.search, navigate]);

  const save = async (): Promise<void> => {
    if (!dirty || saving) return;
    if (!revisionId) {
      setError('当前正文还没有可用于安全保存的修订版本。请先生成候选，或刷新页面同步最新修订后再保存。');
      setSaveMessage('缺少修订版本，未发送保存请求');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = await api.updateChapterDraft(project.project_id, chapter, {
        draft_text: text,
        base_revision_id: revisionId,
      });
      setDraft(saved);
      setText(saved.draft_text);
      setDirty(false);
      setCandidatesHistorical(Boolean(runDetail && saved.draft_text !== runDetail.draft_text));
      setSaveMessage(
        saved.revision_round == null
          ? '正文修订已保存'
          : `第 ${saved.revision_round} 次修订已保存`,
      );
      const [nextAlignment, nextVersions] = await Promise.all([
        optional(api.getChapterAlignment(project.project_id, chapter)),
        optional(api.getChapterPlanVersions(project.project_id, chapter)),
      ]);
      setAlignment(nextAlignment);
      setVersions(nextVersions ?? []);
    } catch (saveError) {
      let hint = saveError instanceof ApiError && saveError.status === 404
        ? '正文保存接口尚未启用；生成流水线仍可落盘正文。'
        : errorMessage(saveError, '正文保存失败');
      if (saveError instanceof ApiError && saveError.status === 409) {
        hint = '服务端已产生更新的正文修订。当前修改仍保留在编辑器中；请核对后再次保存。';
        try {
          const latest = await api.getChapterDraft(project.project_id, chapter);
          setDraft(latest);
        } catch {
          setDraft(null);
          hint += ' 最新修订版本同步失败，请刷新页面。';
        }
      }
      setError(hint);
      setSaveMessage('保存失败，正文仍保留在当前页面');
    } finally {
      setSaving(false);
    }
  };

  const reconcileGeneration = useCallback<GenerationReconcile>(async (
    token: number,
    baselineRunIds: Set<string>,
    source: GenerationReconcileSource,
    targetRunId?: string,
  ): Promise<void> => {
    let sawBackendRun = targetRunId != null;
    let attempt = 0;
    while (generationReconcileTokenRef.current === token) {
      await wait(1500);
      if (generationReconcileTokenRef.current !== token) return;
      try {
        const runs = await api.listPipelineRuns(project.project_id, 100);
        const newRun = targetRunId
          ? runs.find((run) => run.chapter === chapter && run.run_id === targetRunId)
          : runs.find((run) => run.chapter === chapter && !baselineRunIds.has(run.run_id));
        if (newRun) {
          sawBackendRun = true;
          setSaveMessage(
            isTerminalRunStatus(newRun.status)
              ? '后台生成已结束，正在同步本章…'
              : source === 'recovered'
                ? '刷新后已恢复对账；后台仍在生成，本章暂不可重复运行'
                : '页面已停止等待；后台仍在生成，本章暂不可重复运行',
          );
          if (isTerminalRunStatus(newRun.status)) {
            knownRunIdsRef.current = new Set(runs.map((run) => run.run_id));
            const synced = await load();
            if (generationReconcileTokenRef.current !== token) return;
            if (!synced) {
              setGenerationError('后台任务已结束，但最新结果同步失败；本章保持锁定并继续重试。');
              attempt += 1;
              continue;
            }
            setAwaitingGenerationSettlement(false);
            if (newRun.status.toLowerCase() === 'completed') {
              setGenerationFinished(true);
              setGenerationError(
                source === 'recovered'
                  ? '刷新前的后台任务已完成，最新结果已同步。'
                  : '已停止等待页面进度；后台任务随后完成，最新结果已同步。',
              );
              navigateToStage('#candidates');
            } else {
              setGenerationFinished(false);
              setGenerationError(`后台任务已结束（${sessionStatusLabel(newRun.status)}），当前正文未被新结果替换。`);
            }
            return;
          }
        } else if (!targetRunId && !sawBackendRun && attempt >= 3) {
          const synced = await load();
          if (generationReconcileTokenRef.current !== token) return;
          if (!synced) {
            setGenerationError('后台生成状态暂时无法核对，正在重试。');
            attempt += 1;
            continue;
          }
          setAwaitingGenerationSettlement(false);
          setGenerationFinished(false);
          setGenerationError('生成请求没有进入后台运行队列，可以重新尝试。');
          return;
        }
      } catch (reconcileError) {
        setGenerationError(errorMessage(reconcileError, '后台生成状态暂时无法核对，正在重试。'));
      }
      attempt += 1;
      if (attempt === 40) {
        setGenerationError('后台任务仍未返回终态。为避免重复生成，本章继续保持锁定，页面会持续核对。');
      }
    }
  }, [chapter, load, navigateToStage, project.project_id]);

  generationReconcileRef.current = reconcileGeneration;

  const generate = async (): Promise<void> => {
    if (dirty) {
      setGenerationError('请先保存当前正文修改，再生成新的候选稿。');
      return;
    }
    if (generating || awaitingGenerationSettlement) return;
    setGenerating(true);
    const abortController = new AbortController();
    generationAbortRef.current = abortController;
    generationBaselineRunIdsRef.current = new Set(knownRunIdsRef.current);
    generationReconcileTokenRef.current += 1;
    setGenerationFinished(false);
    setGenerationStages([]);
    setGenerationError(null);
    try {
      await api.runPipelineStream(
        project.project_id,
        {
          chapter_no: chapter,
          chapter_goal: contract?.goal ?? undefined,
          mode: 'human_gate',
          n_candidates: 3,
          quality_check: true,
        },
        {
          onStage: (stage) => setGenerationStages((previous) => [...previous, stage]),
          onDone: (done) => {
            if (done.error) {
              setGenerationFinished(false);
              setGenerationError(userFacingError(new Error(done.error), '生成失败，请稍后重试。'));
              return;
            }
            setGenerationFinished(true);
            // The pipeline has persisted a new revision. Invalidate the old
            // optimistic-lock token until the authoritative draft is fetched.
            setDraft(null);
            setText(done.draft_text ?? '');
            setDirty(false);
            setSaveMessage('生成稿已落盘，正在同步最新修订版本…');
            if (done.alignment_report) setAlignment(done.alignment_report);
            const syncRun = async (): Promise<void> => {
              try {
                let fallbackText = done.draft_text ?? '';
                if (done.run_id) {
                  const detail = await api.getPipelineRun(project.project_id, done.run_id);
                  setRunDetail(detail);
                  knownRunIdsRef.current.add(done.run_id);
                  fallbackText = detail.draft_text || fallbackText;
                  if (detail.alignment_report) setAlignment(detail.alignment_report);
                }
                await syncLatestDraft(fallbackText);
                setCandidatesHistorical(false);
                navigateToStage('#candidates');
              } catch (syncError) {
                setDraft(null);
                setSaveMessage('生成稿已落盘，但最新修订版本同步失败；刷新后再编辑保存');
                setGenerationError(errorMessage(syncError, '生成完成，但正文修订同步失败'));
              }
            };
            void syncRun();
          },
          onError: (streamError) => {
            setGenerationFinished(false);
            setGenerationError(
              userFacingError(new Error(streamError.message || '生成失败'), '生成失败，请稍后重试。'),
            );
          },
        },
        abortController.signal,
      );
    } catch (generateError) {
      if (generateError instanceof DOMException && generateError.name === 'AbortError') {
        setGenerationFinished(false);
        setGenerationError('已停止等待页面进度；后台任务若已开始仍会继续，本章正在对账。');
      } else {
        setGenerationFinished(false);
        setGenerationError(errorMessage(generateError, '生成失败'));
      }
    } finally {
      generationAbortRef.current = null;
      setGenerating(false);
    }
  };

  const cancelGeneration = (): void => {
    if (!generationAbortRef.current || awaitingGenerationSettlement) return;
    const token = generationReconcileTokenRef.current + 1;
    generationReconcileTokenRef.current = token;
    setAwaitingGenerationSettlement(true);
    setSaveMessage('已停止等待页面进度；正在核对后台任务状态');
    generationAbortRef.current?.abort();
    void reconcileGeneration(
      token,
      new Set(generationBaselineRunIdsRef.current),
      'stopped',
    );
  };

  const selectCandidate = async (index: number): Promise<void> => {
    if (
      !runDetail
      || dirty
      || candidatesHistorical
      || generating
      || awaitingGenerationSettlement
      || selecting != null
    ) return;
    setSelecting(index);
    setGenerationError(null);
    try {
      const latestDraft = await api.getChapterDraft(project.project_id, chapter);
      if (latestDraft.revision_id !== revisionId) {
        setDraft(latestDraft);
        setText(latestDraft.draft_text);
        setDirty(false);
        setCandidatesHistorical(true);
        setSaveMessage('检测到更新的服务端修订；历史候选已切换为只读');
        throw new Error('服务端正文已更新，未采用旧候选。请先核对最新正文。');
      }
      const detail = await api.selectCandidate(project.project_id, runDetail.run_id, index);
      setRunDetail(detail);
      setCandidatesHistorical(false);
      setDraft(null);
      setText(detail.draft_text);
      setDirty(false);
      setSaveMessage('人工选稿已落盘，正在同步最新修订版本…');
      try {
        await syncLatestDraft(detail.draft_text);
      } catch (syncError) {
        setDraft(null);
        setSaveMessage('选稿已落盘，但最新修订版本同步失败；刷新后再编辑保存');
        setGenerationError(errorMessage(syncError, '选稿成功，但正文修订同步失败'));
      }
      if (detail.alignment_report) {
        setAlignment(detail.alignment_report);
      } else {
        try {
          setAlignment(await optional(api.getChapterAlignment(project.project_id, chapter)));
        } catch (alignmentError) {
          setGenerationError(errorMessage(alignmentError, '选稿已落盘，但偏航报告同步失败'));
        }
      }
      setInspectorTab('alignment');
      navigateToStage('#proofread');
    } catch (selectError) {
      setGenerationError(errorMessage(selectError, '候选稿校验失败'));
    } finally {
      setSelecting(null);
    }
  };

  const card = cards.find((item) => item.chapter === chapter);
  const title = contract?.title || card?.title || `第 ${chapter} 章`;
  const candidateReadOnlyReason = generating
    ? '新候选生成中；上一轮候选仅供对照，完成前不可采用。'
    : awaitingGenerationSettlement
      ? '后台对账中；上一轮候选仅供对照，结果确认前不可采用。'
      : dirty
        ? '当前正文有未保存修改。保存或放弃修改后，才能采用候选稿。'
        : candidatesHistorical
          ? '这些候选来自较早的生成记录；当前正文已有更新修订，为避免覆盖只保留对照阅读。'
          : null;
  const canEdit = Boolean(revisionId) && !generating && !awaitingGenerationSettlement && selecting == null;
  const canContinue = Boolean(
    revisionId
    && text.trim()
    && !dirty
    && !generating
    && !awaitingGenerationSettlement
    && selecting == null,
  );

  return (
    <div className="writing-workspace">
      <StorySpine
        projectId={project.project_id}
        chapters={storyMap.chapters}
        volumes={storyMap.volumes}
        currentChapter={chapter}
        workspaceSearch={workspaceSearch}
      />

      <div className="writing-center">
        {error && <div className="workspace-banner" role="alert">{error}</div>}
        {loading ? (
          <div className="workspace-loading"><span className="nf-spin" /> 正在铺开第 {chapter} 章稿纸…</div>
        ) : (
          <>
            <ChapterEditor
              chapter={chapter}
              title={title}
              goal={contract?.goal ?? card?.goal}
              text={text}
              textareaRef={editorRef}
              dirty={dirty}
              canSave={Boolean(revisionId)}
              saving={saving}
              saveMessage={saveMessage}
              generating={generating}
              awaitingGenerationSettlement={awaitingGenerationSettlement}
              generationFinished={generationFinished}
              generationStages={generationStages}
              generationError={generationError}
              contentLanguage={project.content_language}
              onCancelGeneration={cancelGeneration}
              canContinue={canContinue}
              canEdit={canEdit}
              onContinue={() => navigate(`/studio/${encodeURIComponent(project.project_id)}/write/${chapter + 1}${workspaceSearch}`)}
              onChange={(value) => { setText(value); setDirty(true); setSaveMessage('有未保存修改'); }}
              onSave={() => void save()}
              onGenerate={() => void generate()}
            />
            <CandidateProofs
              detail={runDetail}
              contentLanguage={project.content_language}
              selecting={selecting}
              readOnlyReason={candidateReadOnlyReason}
              onSelect={(index) => void selectCandidate(index)}
            />
          </>
        )}
      </div>

      <aside className="chapter-inspector" id="proofread" aria-label="章节契约与偏航检查">
        <div className="inspector-tabs" aria-label="章节检查面板">
          <button
            type="button"
            aria-pressed={inspectorTab === 'contract'}
            className={inspectorTab === 'contract' ? 'active' : ''}
            onClick={() => {
              setInspectorTab('contract');
              navigateToStage('');
            }}
          >
            接下来 · 章节契约
          </button>
          <button
            type="button"
            aria-pressed={inspectorTab === 'alignment'}
            className={inspectorTab === 'alignment' ? 'active' : ''}
            onClick={() => {
              setInspectorTab('alignment');
              navigateToStage('#proofread');
            }}
          >
            已发生 · 偏航检查
            {alignment && <span>{Math.round(alignment.divergence_score * 100)}%</span>}
          </button>
        </div>
        <div className="inspector-scroll">
          {inspectorTab === 'contract' ? (
            <ContractInspector contract={contract} loading={loading} />
          ) : (
            <AlignmentReportPanel
              report={alignment}
              loading={loading}
              onRevise={() => {
                navigateToStage('');
                editorRef.current?.focus();
              }}
              onReplan={() => setReplanOpen(true)}
              onKeep={() => {
                setSaveMessage('校对完成：保持原计划，可以继续下一章');
                navigateToStage('#continue');
              }}
            />
          )}
          <PlanVersionTimeline versions={versions} />
        </div>
      </aside>

      <ReplanDialog
        projectId={project.project_id}
        chapter={chapter}
        open={replanOpen}
        onClose={() => setReplanOpen(false)}
        onApplied={() => void load()}
      />

      {blocker.state === 'blocked' && (
        <div className="writing-navigation-guard" role="presentation">
          <section role="dialog" aria-modal="true" aria-labelledby="writing-navigation-title">
            <span>确认离开本章？</span>
            <h2 id="writing-navigation-title">当前工作还没有安全落盘</h2>
            <p>
              {dirty
                ? '正文还有未保存修改。直接离开会丢失这些内容。'
                : awaitingGenerationSettlement
                  ? '后台生成仍在对账。现在离开不会取消任务，稍后回来会重新同步。'
                  : generating
                    ? '本章正在生成。离开页面不会可靠地取消后台任务。'
                    : '候选稿正在采用并同步，请等这一轮完成。'}
            </p>
            <div>
              <button type="button" autoFocus onClick={() => blocker.reset()}>留在本章</button>
              <button type="button" className="danger" onClick={() => blocker.proceed()}>仍然离开</button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
