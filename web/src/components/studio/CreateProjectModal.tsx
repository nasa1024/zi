// CreateProjectModal — 新建项目弹窗。
// 真实调用 api.createProject；Esc / 点遮罩关闭；loading / 错误态。
// open=false 时返回 null。

import { useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type { ProjectCreateRequest, ProjectResponse } from '../../api/types';

export interface CreateProjectModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (p: ProjectResponse) => void;
}

interface GenreOption {
  value: string;
  label: string;
}

const GENRE_OPTIONS: GenreOption[] = [
  { value: 'xuanhuan', label: '玄幻 · xuanhuan' },
  { value: 'xianxia', label: '修仙 · xianxia' },
  { value: 'dushi', label: '都市 · dushi' },
  { value: 'kehuan', label: '科幻 · kehuan' },
  { value: 'lishi', label: '历史 · lishi' },
  { value: 'wuxia', label: '武侠 · wuxia' },
  { value: 'yanqing', label: '言情 · yanqing' },
  { value: 'xuanyi', label: '悬疑 · xuanyi' },
  { value: 'default', label: '通用 · default' },
];

export default function CreateProjectModal({
  open,
  onClose,
  onCreated,
}: CreateProjectModalProps): JSX.Element | null {
  const [name, setName] = useState<string>('');
  const [genre, setGenre] = useState<string>('xuanhuan');
  const [powerSystem, setPowerSystem] = useState<string>('');
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Reset form whenever the modal is (re)opened.
  useEffect(() => {
    if (open) {
      setName('');
      setGenre('xuanhuan');
      setPowerSystem('');
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  // Esc to close (only while open and not submitting).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, submitting, onClose]);

  if (!open) {
    return null;
  }

  const trimmedName = name.trim();
  const canSubmit = trimmedName.length > 0 && !submitting;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    const req: ProjectCreateRequest = {
      name: trimmedName,
      genre,
      power_system: powerSystem.trim() ? powerSystem.trim() : null,
    };
    try {
      const created = await api.createProject(req);
      onCreated(created);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('创建项目失败');
      }
      setSubmitting(false);
    }
  };

  const handleOverlayClick = () => {
    if (!submitting) onClose();
  };

  return (
    <div className="modal-overlay" onMouseDown={handleOverlayClick}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-label="新建项目"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mc-head">
          <div>
            <span className="mc-eyebrow">NEW PROJECT</span>
            <h3 className="cn">＋ 开一本新书</h3>
          </div>
          <button
            type="button"
            className="modal-close"
            aria-label="关闭"
            onClick={onClose}
            disabled={submitting}
          >
            ✕
          </button>
        </div>

        <form className="nf-form single" onSubmit={handleSubmit}>
          <div className="nf-field full">
            <label htmlFor="cp-name">
              书名 / NAME<span className="req">*</span>
            </label>
            <input
              id="cp-name"
              className="nf-input"
              type="text"
              placeholder="例如：万古第一剑修"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              disabled={submitting}
            />
          </div>

          <div className="nf-field full">
            <label htmlFor="cp-genre">题材 / GENRE</label>
            <select
              id="cp-genre"
              className="nf-select"
              value={genre}
              onChange={(e) => setGenre(e.target.value)}
              disabled={submitting}
            >
              {GENRE_OPTIONS.map((g) => (
                <option key={g.value} value={g.value}>
                  {g.label}
                </option>
              ))}
            </select>
          </div>

          <div className="nf-field full">
            <label htmlFor="cp-power">力量体系 / POWER SYSTEM（可选）</label>
            <input
              id="cp-power"
              className="nf-input"
              type="text"
              placeholder="例如：炼气→筑基→金丹→元婴…"
              value={powerSystem}
              onChange={(e) => setPowerSystem(e.target.value)}
              disabled={submitting}
            />
          </div>

          {error && (
            <div className="nf-msg err" role="alert">
              <span>⚠</span>
              <span>{error}</span>
            </div>
          )}

          <div className="nf-actions">
            <button type="submit" className="nf-btn pink" disabled={!canSubmit}>
              {submitting ? (
                <>
                  <span className="nf-spin" /> 创建中…
                </>
              ) : (
                <>⚡ 创建项目</>
              )}
            </button>
            <button
              type="button"
              className="nf-btn ghost"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
