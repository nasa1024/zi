import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
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
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}

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
  const chapter = Math.max(1, Number.parseInt(chapterParam, 10) || 1);
  const editorRef = useRef<HTMLTextAreaElement>(null);

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
  const [generationStages, setGenerationStages] = useState<SSEStageEvent[]>([]);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<PipelineRunDetail | null>(null);
  const [selecting, setSelecting] = useState<number | null>(null);
  const [inspectorTab, setInspectorTab] = useState<'contract' | 'alignment'>('contract');
  const [replanOpen, setReplanOpen] = useState(false);

  const load = useCallback(async (): Promise<void> => {
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

      let recoveredDraft = serverDraft;
      const latestRun = runs.find((run) => run.chapter === chapter && run.status === 'completed');
      if (!recoveredDraft && latestRun) {
        const detail = await api.getPipelineRun(project.project_id, latestRun.run_id);
        recoveredDraft = {
          chapter,
          draft_text: detail.draft_text,
          word_count: detail.word_count,
        };
        setRunDetail(detail);
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
        recoveredDraft?.revision_id
          ? '正文已与服务端同步'
          : recoveredDraft?.draft_text
            ? '已恢复旧稿；生成候选后会建立可编辑修订'
            : '尚无正文修订；先生成候选再编辑保存',
      );
    } catch (loadError) {
      setError(errorMessage(loadError, '写作工作区加载失败'));
    } finally {
      setLoading(false);
    }
  }, [chapter, project.project_id]);

  useEffect(() => { void load(); }, [load]);

  const syncLatestDraft = useCallback(async (fallbackText = ''): Promise<ChapterDraft> => {
    const latest = await api.getChapterDraft(project.project_id, chapter);
    setDraft(latest);
    setText(latest.draft_text || fallbackText);
    setDirty(false);
    setSaveMessage(
      latest.revision_id
        ? `已同步修订 ${latest.revision_round ?? ''}`.trim()
        : '正文已落盘，但服务端尚未返回可编辑 revision',
    );
    return latest;
  }, [chapter, project.project_id]);

  const revisionId = draft?.revision_id ?? null;

  const save = async (): Promise<void> => {
    if (!dirty || saving) return;
    if (!revisionId) {
      setError('当前正文还没有可并发校验的 revision。请先生成候选，或刷新页面同步最新修订后再保存。');
      setSaveMessage('缺少 revision，未发送保存请求');
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
      setSaveMessage(`修订 ${saved.revision_round ?? '已'} 落盘`);
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
          hint += ' 最新 revision 同步失败，请刷新页面。';
        }
      }
      setError(hint);
      setSaveMessage('保存失败，正文仍保留在当前页面');
    } finally {
      setSaving(false);
    }
  };

  const generate = async (): Promise<void> => {
    setGenerating(true);
    setGenerationStages([]);
    setGenerationError(null);
    setRunDetail(null);
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
              setGenerationError(done.error);
              return;
            }
            // The pipeline has persisted a new revision. Invalidate the old
            // optimistic-lock token until the authoritative draft is fetched.
            setDraft(null);
            setText(done.draft_text ?? '');
            setDirty(false);
            setSaveMessage('生成稿已落盘，正在同步最新 revision…');
            if (done.alignment_report) setAlignment(done.alignment_report);
            const syncRun = async (): Promise<void> => {
              try {
                let fallbackText = done.draft_text ?? '';
                if (done.run_id) {
                  const detail = await api.getPipelineRun(project.project_id, done.run_id);
                  setRunDetail(detail);
                  fallbackText = detail.draft_text || fallbackText;
                  if (detail.alignment_report) setAlignment(detail.alignment_report);
                }
                await syncLatestDraft(fallbackText);
              } catch (syncError) {
                setDraft(null);
                setSaveMessage('生成稿已落盘，但最新 revision 同步失败；刷新后再编辑保存');
                setGenerationError(errorMessage(syncError, '生成完成，但正文修订同步失败'));
              }
            };
            void syncRun();
            setInspectorTab('alignment');
          },
          onError: (streamError) => setGenerationError(streamError.message || '生成失败'),
        },
      );
    } catch (generateError) {
      setGenerationError(errorMessage(generateError, '生成失败'));
    } finally {
      setGenerating(false);
    }
  };

  const selectCandidate = async (index: number): Promise<void> => {
    if (!runDetail) return;
    setSelecting(index);
    setGenerationError(null);
    try {
      const detail = await api.selectCandidate(project.project_id, runDetail.run_id, index);
      setRunDetail(detail);
      setDraft(null);
      setText(detail.draft_text);
      setDirty(false);
      setSaveMessage('人工选稿已落盘，正在同步最新 revision…');
      try {
        await syncLatestDraft(detail.draft_text);
      } catch (syncError) {
        setDraft(null);
        setSaveMessage('选稿已落盘，但最新 revision 同步失败；刷新后再编辑保存');
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
    } catch (selectError) {
      setGenerationError(errorMessage(selectError, '候选稿校验失败'));
    } finally {
      setSelecting(null);
    }
  };

  const card = cards.find((item) => item.chapter === chapter);
  const title = contract?.title || card?.title || `第 ${chapter} 章`;

  return (
    <div className="writing-workspace">
      <StorySpine
        projectId={project.project_id}
        chapters={storyMap.chapters}
        volumes={storyMap.volumes}
        currentChapter={chapter}
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
              generationStages={generationStages}
              generationError={generationError}
              onChange={(value) => { setText(value); setDirty(true); setSaveMessage('有未保存修改'); }}
              onSave={() => void save()}
              onGenerate={() => void generate()}
            />
            <CandidateProofs detail={runDetail} selecting={selecting} onSelect={(index) => void selectCandidate(index)} />
          </>
        )}
      </div>

      <aside className="chapter-inspector" aria-label="章节契约与偏航检查">
        <div className="inspector-tabs" role="tablist" aria-label="章节检查面板">
          <button
            type="button"
            role="tab"
            aria-selected={inspectorTab === 'contract'}
            className={inspectorTab === 'contract' ? 'active' : ''}
            onClick={() => setInspectorTab('contract')}
          >
            章节契约
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inspectorTab === 'alignment'}
            className={inspectorTab === 'alignment' ? 'active' : ''}
            onClick={() => setInspectorTab('alignment')}
          >
            偏航检查
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
              onRevise={() => editorRef.current?.focus()}
              onReplan={() => setReplanOpen(true)}
              onKeep={() => setSaveMessage('保持原计划；请按契约继续修订正文')}
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
    </div>
  );
}
