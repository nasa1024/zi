import { Link } from 'react-router-dom';
import type { StoryMapChapter, VolumeInfo } from '../../api/types';

interface StorySpineProps {
  projectId: string;
  chapters: StoryMapChapter[];
  volumes: VolumeInfo[];
  currentChapter: number;
}

function volumeForChapter(volumes: VolumeInfo[], chapter: number): VolumeInfo | null {
  return volumes.find((volume) => (
    volume.start_chapter != null
    && volume.start_chapter <= chapter
    && (volume.end_chapter == null || volume.end_chapter >= chapter)
  )) ?? null;
}

export default function StorySpine({
  projectId,
  chapters,
  volumes,
  currentChapter,
}: StorySpineProps): JSX.Element {
  const rows = chapters.slice().sort((a, b) => a.chapter - b.chapter);
  if (!rows.some((item) => item.chapter === currentChapter)) {
    rows.push({
      chapter: currentChapter,
      title: null,
      goal: null,
      hook_text: null,
      status: 'unplanned',
      plan_version: 0,
      locked: false,
    });
    rows.sort((a, b) => a.chapter - b.chapter);
  }
  const currentVolume = volumeForChapter(volumes, currentChapter);
  const visible = rows.filter((item) => Math.abs(item.chapter - currentChapter) <= 12);

  return (
    <aside className="story-spine" aria-label="故事脊柱">
      <div className="story-spine-head">
        <span>STORY SPINE</span>
        <h2>故事脊柱</h2>
        <p>{currentVolume ? `第 ${currentVolume.volume_no} 卷 · ${currentVolume.title}` : '尚未归入卷'}</p>
      </div>

      <nav className="story-spine-track" aria-label="章节导航">
        {visible.map((item) => {
          const active = item.chapter === currentChapter;
          const status = item.status || 'planned';
          return (
            <Link
              key={item.chapter}
              to={`/studio/${encodeURIComponent(projectId)}/write/${item.chapter}`}
              aria-current={active ? 'page' : undefined}
              className={`spine-chapter ${status}${active ? ' active' : ''}`}
            >
              <span className="spine-node" aria-hidden="true" />
              <span className="spine-number">{String(item.chapter).padStart(2, '0')}</span>
              <span className="spine-copy">
                <b>{item.title || `第 ${item.chapter} 章`}</b>
                <small>
                  {status === 'drafted' ? '正文已落盘' : status === 'planned' ? '等待写作' : '等待规划'}
                  {item.plan_version > 0 && ` · 计划 v${item.plan_version}`}
                </small>
              </span>
              {Boolean(item.locked) && <span className="spine-lock" title="已锁定章节契约">LOCK</span>}
            </Link>
          );
        })}
      </nav>

      <div className="story-spine-actions">
        <Link
          className={`spine-step${currentChapter <= 1 ? ' disabled' : ''}`}
          aria-disabled={currentChapter <= 1}
          to={`/studio/${encodeURIComponent(projectId)}/write/${Math.max(1, currentChapter - 1)}`}
        >
          ← 上一章
        </Link>
        <Link
          className="spine-step"
          to={`/studio/${encodeURIComponent(projectId)}/write/${currentChapter + 1}`}
        >
          下一章 →
        </Link>
      </div>
    </aside>
  );
}
