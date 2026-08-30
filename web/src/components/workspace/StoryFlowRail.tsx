import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import '../../styles/story-flow.css';

const STORY_FLOW_KEYS = [
  'start',
  'direction',
  'future',
  'write',
  'select',
  'proofread',
  'continue',
] as const;

export type StoryFlowStepKey = (typeof STORY_FLOW_KEYS)[number];
export type StoryFlowStep = StoryFlowStepKey | number;

export interface StoryFlowRailProps {
  projectId: string;
  currentChapter: number;
  /** Preserve an unresolved Agent checkpoint while moving inside the workspace. */
  workspaceSearch?: string;
  /** String keys are preferred; numeric values are zero-based indexes. */
  currentStep?: StoryFlowStep | null;
}

interface StoryFlowStepDefinition {
  key: StoryFlowStepKey;
  label: string;
  assistiveDescription: string;
  to: string;
}

const STEP_ALIASES: Record<string, StoryFlowStepKey> = {
  start: 'start',
  begin: 'start',
  开始: 'start',
  direction: 'direction',
  orient: 'direction',
  planning: 'direction',
  定方向: 'direction',
  future: 'future',
  'future-plan': 'future',
  plan: 'future',
  排未来: 'future',
  write: 'write',
  draft: 'write',
  chapter: 'write',
  写本章: 'write',
  select: 'select',
  candidate: 'select',
  candidates: 'select',
  选稿: 'select',
  proofread: 'proofread',
  review: 'proofread',
  check: 'proofread',
  校对: 'proofread',
  continue: 'continue',
  next: 'continue',
  继续: 'continue',
};

function normalizeChapter(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.trunc(value));
}

function normalizeStep(value: StoryFlowStep | null | undefined): StoryFlowStepKey | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const index = Math.min(Math.max(0, Math.trunc(value)), STORY_FLOW_KEYS.length - 1);
    return STORY_FLOW_KEYS[index];
  }

  if (typeof value === 'string') {
    return STEP_ALIASES[value.trim().toLowerCase()] ?? 'start';
  }

  return null;
}

function buildSteps(projectId: string, chapter: number, workspaceSearch = ''): StoryFlowStepDefinition[] {
  const workspace = `/studio/${encodeURIComponent(projectId)}`;
  const withSearch = (path: string, hash = '') => `${workspace}/${path}${workspaceSearch}${hash}`;

  return [
    {
      key: 'start',
      label: '开始',
      assistiveDescription: '从书架选择要继续的作品',
      to: '/studio',
    },
    {
      key: 'direction',
      label: '定方向',
      assistiveDescription: '确认全书方向与本卷目标',
      to: withSearch('plan'),
    },
    {
      key: 'future',
      label: '排未来',
      assistiveDescription: '排好接下来几章的故事路线',
      to: withSearch('plan', '#future'),
    },
    {
      key: 'write',
      label: '写本章',
      assistiveDescription: `打开第 ${chapter} 章，开始落笔`,
      to: withSearch(`write/${chapter}`),
    },
    {
      key: 'select',
      label: '选稿',
      assistiveDescription: `比较第 ${chapter} 章的候选稿`,
      to: withSearch(`write/${chapter}`, '#candidates'),
    },
    {
      key: 'proofread',
      label: '校对',
      assistiveDescription: '检查正文是否遵守故事契约',
      to: withSearch(`write/${chapter}`, '#proofread'),
    },
    {
      key: 'continue',
      label: '继续',
      assistiveDescription: `带着本章结果继续到第 ${chapter + 1} 章`,
      to: withSearch(`write/${chapter + 1}`),
    },
  ];
}

export default function StoryFlowRail({
  projectId,
  currentChapter,
  workspaceSearch = '',
  currentStep,
}: StoryFlowRailProps): JSX.Element {
  const chapter = normalizeChapter(currentChapter);
  const activeKey = normalizeStep(currentStep);
  const activeIndex = activeKey == null ? -1 : STORY_FLOW_KEYS.indexOf(activeKey);
  const steps = buildSteps(projectId, chapter, workspaceSearch);
  const listRef = useRef<HTMLOListElement>(null);
  const currentLinkRef = useRef<HTMLAnchorElement>(null);

  useEffect(() => {
    const list = listRef.current;
    const currentLink = currentLinkRef.current;
    if (!list || !currentLink || list.scrollWidth <= list.clientWidth) return;

    const listRect = list.getBoundingClientRect();
    const linkRect = currentLink.getBoundingClientRect();
    const requestedLeft = list.scrollLeft
      + linkRect.left
      - listRect.left
      - ((listRect.width - linkRect.width) / 2);
    const nextLeft = Math.max(0, Math.min(requestedLeft, list.scrollWidth - list.clientWidth));
    list.scrollTo({ left: nextLeft, behavior: 'auto' });
  }, [activeKey]);

  return (
    <nav className="story-flow-rail" aria-label="小说创作七步">
      <span className="story-flow-context">
        第 {chapter} 章 · 创作流程
      </span>

      <ol ref={listRef} className={`story-flow-list${activeKey ? ' has-current' : ''}`}>
        {steps.map((step, index) => {
          const isCurrent = step.key === activeKey;
          const isComplete = index < activeIndex;
          const state = isCurrent ? '当前步骤' : isComplete ? '已完成' : activeKey == null ? '可进入' : '待进行';

          return (
            <li
              className={`story-flow-item${isCurrent ? ' is-current' : ''}${isComplete ? ' is-complete' : ''}`}
              data-step={step.key}
              key={step.key}
            >
              <Link
                ref={isCurrent ? currentLinkRef : undefined}
                className={`story-flow-link${isCurrent ? ' is-current' : ''}${isComplete ? ' is-complete' : ''}`}
                aria-current={isCurrent ? 'step' : undefined}
                aria-label={`${step.label}：${step.assistiveDescription}（${state}）`}
                to={step.to}
              >
                <span className="story-flow-marker" aria-hidden="true">
                  {index + 1}
                </span>
                <span className="story-flow-label">{step.label}</span>
              </Link>
              {index < steps.length - 1 && <span className="story-flow-connector" aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
