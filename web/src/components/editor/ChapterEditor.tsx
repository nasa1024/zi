import type { RefObject } from 'react';
import type { SSEStageEvent } from '../../api/types';
import { countContentUnits } from '../../utils/contentCount';

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
  generationStages: SSEStageEvent[];
  generationError?: string | null;
  contentLanguage?: string | null;
  onChange: (value: string) => void;
  onSave: () => void;
  onGenerate: () => void;
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
  generationStages,
  generationError,
  contentLanguage,
  onChange,
  onSave,
  onGenerate,
}: ChapterEditorProps): JSX.Element {
  const count = countContentUnits(text, contentLanguage);
  const numberLocale = contentLanguage?.startsWith('en') ? 'en-US' : 'zh-CN';

  return (
    <main className="manuscript-desk">
      <header className="manuscript-head">
        <div className="manuscript-number" aria-hidden="true">{String(chapter).padStart(2, '0')}</div>
        <div className="manuscript-title">
          <span>CHAPTER {String(chapter).padStart(2, '0')} · LIVE MANUSCRIPT</span>
          <h1>{title || `第 ${chapter} 章`}</h1>
          {goal && <p>{goal}</p>}
        </div>
        <div className="manuscript-actions">
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
            disabled={generating}
          >
            {generating ? <><span className="nf-spin" /> 正在锻稿</> : '✦ 生成候选'}
          </button>
        </div>
      </header>

      <div className="manuscript-meta" aria-live="polite">
        <span>{count.value.toLocaleString(numberLocale)} {count.unit === 'words' ? 'words' : '字符'}</span>
        <span>{saveMessage}</span>
        <span>⌘/Ctrl + S 保存</span>
      </div>

      {generationStages.length > 0 && (
        <div className="forge-progress" aria-live="polite" aria-label="章节生成进度">
          {generationStages.map((stage, index) => (
            <span className={`forge-stage ${stage.status}`} key={`${stage.stage}-${index}`}>
              <i aria-hidden="true" /> {stage.stage}
            </span>
          ))}
        </div>
      )}

      {generationError && <div className="desk-alert" role="alert">{generationError}</div>}

      <div className="manuscript-paper">
        <div className="paper-margin" aria-hidden="true">DRAFT / CANON PENDING</div>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
              event.preventDefault();
              onSave();
            }
          }}
          aria-label={`第 ${chapter} 章正文`}
          spellCheck
          placeholder="在这里落下本章第一句，或使用「生成候选」让引擎先锻出三份校样。"
        />
      </div>
    </main>
  );
}
