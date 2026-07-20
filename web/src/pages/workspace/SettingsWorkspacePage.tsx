import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type {
  GenreCatalogItem,
  ProjectWritingProfilePatch,
  ProjectWritingProfileResponse,
  WritingProfileCatalogItem,
} from '../../api/types';
import MemoryPanel from '../../components/studio/MemoryPanel';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';

function message(error: unknown, fallback: string): string {
  return error instanceof ApiError || error instanceof Error ? error.message : fallback;
}

function splitList(value: string): string[] {
  return Array.from(new Set(value.split(/[,，]/).map((item) => item.trim()).filter(Boolean)));
}

export default function SettingsWorkspacePage(): JSX.Element {
  const { project, refetchProjects } = useWorkspaceOutlet();
  const [current, setCurrent] = useState<ProjectWritingProfileResponse | null>(null);
  const [profiles, setProfiles] = useState<WritingProfileCatalogItem[]>([]);
  const [genres, setGenres] = useState<GenreCatalogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);

  const [contentLanguage, setContentLanguage] = useState(project.content_language ?? 'zh-CN');
  const [targetMarket, setTargetMarket] = useState('cn_web');
  const [publicationFormat, setPublicationFormat] = useState('web_serial');
  const [primaryGenre, setPrimaryGenre] = useState('');
  const [profileId, setProfileId] = useState('');
  const [profileOverrides, setProfileOverrides] = useState<Record<string, unknown>>({});
  const [storyTagsText, setStoryTagsText] = useState('');
  const [audienceRating, setAudienceRating] = useState('general');
  const [contentWarningsText, setContentWarningsText] = useState('');
  const [effectiveFromChapter, setEffectiveFromChapter] = useState(project.chapter_count + 1);
  const [changeReason, setChangeReason] = useState('');

  const applyCurrent = useCallback((profile: ProjectWritingProfileResponse) => {
    setCurrent(profile);
    setContentLanguage(profile.content_language);
    setTargetMarket(profile.target_market);
    setPublicationFormat(profile.publication_format);
    setPrimaryGenre(profile.primary_genre ?? project.primary_genre ?? project.genre);
    setProfileId(profile.writing_profile_id);
    setProfileOverrides(profile.writing_profile_overrides ?? {});
    setStoryTagsText(profile.story_tags.join(', '));
    setAudienceRating(profile.audience_rating ?? 'general');
    setContentWarningsText(profile.content_warnings.join(', '));
    setEffectiveFromChapter(Math.max(
      project.chapter_count + 1,
      profile.writing_profile_effective_from_chapter ?? project.chapter_count + 1,
    ));
    setChangeReason('');
  }, [project.chapter_count, project.genre, project.primary_genre]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const profile = await api.getWritingProfile(project.project_id);
      applyCurrent(profile);
      const [profileCatalog, genreCatalog] = await Promise.all([
        api.listWritingProfiles(
          profile.content_language,
          profile.publication_format,
          profile.target_market,
        ),
        api.listGenres(profile.content_language, profile.target_market),
      ]);
      setProfiles(profileCatalog);
      setGenres(genreCatalog);
    } catch (loadError) {
      setError(message(loadError, '写作配置加载失败'));
    } finally {
      setLoading(false);
    }
  }, [applyCurrent, project.project_id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!editing || !current) return;
    let cancelled = false;
    void Promise.all([
      api.listWritingProfiles(contentLanguage, publicationFormat, targetMarket),
      api.listGenres(contentLanguage, targetMarket),
    ]).then(([profileCatalog, genreCatalog]) => {
      if (cancelled) return;
      setProfiles(profileCatalog);
      setGenres(genreCatalog);
      setProfileId((value) => profileCatalog.some((item) => item.id === value) ? value : (profileCatalog[0]?.id ?? ''));
      setPrimaryGenre((value) => genreCatalog.some((item) => item.id === value) ? value : (genreCatalog[0]?.id ?? ''));
    }).catch((catalogError: unknown) => {
      if (!cancelled) setError(message(catalogError, '兼容配置目录加载失败'));
    });
    return () => { cancelled = true; };
  }, [contentLanguage, current, editing, publicationFormat, targetMarket]);

  const save = async () => {
    if (!current || !profileId || saving) return;
    setSaving(true);
    setError(null);
    setSavedMessage(null);
    const patch: ProjectWritingProfilePatch = {
      content_language: contentLanguage,
      target_market: targetMarket,
      publication_format: publicationFormat,
      primary_genre: primaryGenre || null,
      story_tags: splitList(storyTagsText),
      audience_rating: audienceRating,
      content_warnings: splitList(contentWarningsText),
      writing_profile_id: profileId,
      writing_profile_overrides: profileOverrides,
      effective_from_chapter: effectiveFromChapter,
      change_reason: changeReason.trim() || null,
    };
    try {
      const commit = async (confirmHighRisk: boolean) => {
        const updated = await api.updateWritingProfile(project.project_id, {
          ...patch,
          confirm_high_risk: confirmHighRisk,
        });
        applyCurrent(updated);
        setEditing(false);
        setSavedMessage(`写作配置 v${updated.writing_profile_version} 已保存，从第 ${updated.writing_profile_effective_from_chapter ?? effectiveFromChapter} 章生效。`);
        refetchProjects();
      };
      await commit(false);
    } catch (saveError) {
      if (
        saveError instanceof ApiError
        && saveError.code === 'high_risk_confirmation_required'
        && window.confirm('这次修改会改变 POV 或时态，只能从下一章起生效。仍要保存新版本吗？')
      ) {
        try {
          const selectedProfileVersion = profiles.find((profile) => profile.id === profileId);
          const confirmed: ProjectWritingProfilePatch = {
            ...patch,
            writing_profile_id: selectedProfileVersion?.id ?? patch.writing_profile_id,
            confirm_high_risk: true,
          };
          const updated = await api.updateWritingProfile(project.project_id, confirmed);
          applyCurrent(updated);
          setEditing(false);
          setSavedMessage(`写作配置 v${updated.writing_profile_version} 已保存，从第 ${updated.writing_profile_effective_from_chapter ?? effectiveFromChapter} 章生效。`);
          refetchProjects();
        } catch (confirmedError) {
          setError(message(confirmedError, '写作配置保存失败'));
        }
      } else {
        setError(message(saveError, '写作配置保存失败'));
      }
    } finally {
      setSaving(false);
    }
  };

  const selectedProfile = profiles.find((profile) => profile.id === profileId) ?? current?.profile ?? null;
  const resolvedRules = {
    ...(selectedProfile?.prompt_rules ?? {}),
    ...profileOverrides,
  };
  const canEditLanguage = project.chapter_count === 0;

  const handleContentLanguageChange = (language: string) => {
    setContentLanguage(language);
    setTargetMarket(language.startsWith('en') ? 'global_english' : 'cn_web');
    setProfileId('');
    setProfileOverrides({});
    setPrimaryGenre('');
  };

  const setProfileOverride = (key: string, value: string) => {
    setProfileOverrides((existing) => {
      const next = { ...existing };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  };

  return (
    <main className="routed-workspace-page settings-route-page">
      <header className="route-page-head">
        <div><span>ENGINE ROOM · PROJECT SCOPE</span><h1>项目设置</h1></div>
        <p>写作配置按版本保存，只影响生效章节之后的生成；已有正文与 Canon 不会被改写。</p>
      </header>

      <section className="writing-profile-panel">
        <div className="panel-head">
          <div><span>LANGUAGE · MARKET · STYLE</span><h2>写作配置</h2><p>后端版本化目录是唯一事实源。</p></div>
          {!loading && current && !editing && <button type="button" className="nf-btn" onClick={() => { setEditing(true); setSavedMessage(null); }}>修改未来章节配置</button>}
        </div>

        {error && <div className="nf-msg err" role="alert"><span>⚠</span><span>{error}</span><button type="button" className="nf-btn-sm" onClick={() => void load()}>重试</button></div>}
        {savedMessage && <div className="nf-msg"><span>✓</span><span>{savedMessage}</span></div>}
        {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在读取写作配置…</div> : current && (
          <>
            <div className="profile-current-card">
              <div><span>CONTENT LANGUAGE</span><b>{current.content_language}</b><small>{canEditLanguage ? '尚无正文，可直接修改作品语言' : '已有正文；MVP 不支持自动迁移语言'}</small></div>
              <div><span>TARGET MARKET</span><b>{current.target_market}</b><small>{current.publication_format}</small></div>
              <div><span>WRITING PROFILE</span><b>{current.profile?.name ?? current.writing_profile_id}</b><small>v{current.writing_profile_version}</small></div>
              <div><span>EFFECTIVE FROM</span><b>CH. {current.writing_profile_effective_from_chapter ?? 1}</b><small>{current.primary_genre || project.genre}</small></div>
            </div>

            {editing && (
              <div className="profile-settings-form">
                <div className="nf-field"><label htmlFor="settings-language">作品语言</label><select id="settings-language" className="nf-select" value={contentLanguage} onChange={(event) => handleContentLanguageChange(event.target.value)} disabled={saving || !canEditLanguage}><option value="zh-CN">简体中文 · zh-CN</option><option value="en-US">English (US) · en-US</option></select></div>
                <div className="nf-field"><label htmlFor="settings-market">目标市场</label><select id="settings-market" className="nf-select" value={targetMarket} onChange={(event) => setTargetMarket(event.target.value)} disabled={saving}>{contentLanguage.startsWith('en') ? <option value="global_english">全球英文读者</option> : <option value="cn_web">中文网文</option>}</select></div>
                <div className="nf-field"><label htmlFor="settings-format">发布形态</label><select id="settings-format" className="nf-select" value={publicationFormat} onChange={(event) => setPublicationFormat(event.target.value)} disabled={saving}><option value="web_serial">Web Serial</option><option value="novel" disabled>Novel（P1）</option></select></div>
                <div className="nf-field"><label htmlFor="settings-genre">主类型</label><select id="settings-genre" className="nf-select" value={primaryGenre} onChange={(event) => setPrimaryGenre(event.target.value)} disabled={saving}>{genres.map((genre) => <option key={genre.id} value={genre.id}>{genre.label}</option>)}</select></div>
                <div className="nf-field"><label htmlFor="settings-profile">风格预设</label><select id="settings-profile" className="nf-select" value={profileId} onChange={(event) => { setProfileId(event.target.value); setProfileOverrides({}); }} disabled={saving}>{profiles.map((profile) => <option key={`${profile.id}:${profile.version}`} value={profile.id}>{profile.name} · v{profile.version}</option>)}</select></div>
                <div className="nf-field full"><label htmlFor="settings-tags">子类型 / 标签</label><input id="settings-tags" className="nf-input" value={storyTagsText} onChange={(event) => setStoryTagsText(event.target.value)} placeholder="progression_fantasy, litrpg" disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-rating">读者分级</label><select id="settings-rating" className="nf-select" value={audienceRating} onChange={(event) => setAudienceRating(event.target.value)} disabled={saving}><option value="general">General</option><option value="young_adult">Young Adult</option><option value="adult">Adult</option></select></div>
                <div className="nf-field"><label htmlFor="settings-warnings">内容警告<span className="setting-optional">（逗号分隔）</span></label><input id="settings-warnings" className="nf-input" value={contentWarningsText} onChange={(event) => setContentWarningsText(event.target.value)} disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-effective">生效章节</label><input id="settings-effective" className="nf-input" type="number" min={project.chapter_count + 1} value={effectiveFromChapter} onChange={(event) => setEffectiveFromChapter(Math.max(project.chapter_count + 1, Number(event.target.value) || project.chapter_count + 1))} disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-reason">变更原因<span className="setting-optional">（选填）</span></label><input id="settings-reason" className="nf-input" value={changeReason} onChange={(event) => setChangeReason(event.target.value)} disabled={saving} /></div>
                {selectedProfile && <div className="profile-settings-preview full"><b>{selectedProfile.name}</b><p>{selectedProfile.description || selectedProfile.preview || '此配置将用于未来的规划、起草与质量检查。'}</p><small>{String(resolvedRules.pov ?? '—')} · {String(resolvedRules.tense ?? '—')} · {String(resolvedRules.pace ?? '—')}</small></div>}
                {[
                  ['pov', '叙事人称', ['third_person_limited', 'first_person', 'third_person_omniscient']],
                  ['tense', '时态', ['past', 'present']],
                  ['pace', '节奏', ['fast', 'balanced', 'slow_burn']],
                  ['prose_density', '文笔密度', ['accessible', 'descriptive', 'literary']],
                  ['dialogue_density', '对话密度', ['low', 'medium', 'high']],
                ].map(([key, label, values]) => (
                  <div className="nf-field" key={key as string}>
                    <label htmlFor={`settings-profile-${key}`}>{label as string}</label>
                    <select id={`settings-profile-${key}`} className="nf-select" value={String(profileOverrides[key as string] ?? '')} onChange={(event) => setProfileOverride(key as string, event.target.value)} disabled={saving}>
                      <option value="">使用预设（{String(selectedProfile?.prompt_rules[key as string] ?? '—')}）</option>
                      {(values as string[]).map((value) => <option key={value} value={value}>{value}</option>)}
                    </select>
                  </div>
                ))}
                <div className="nf-actions full"><button type="button" className="nf-btn pink" onClick={() => void save()} disabled={saving || !profileId}>{saving ? '保存中…' : '保存新版本'}</button><button type="button" className="nf-btn ghost" onClick={() => { applyCurrent(current); setEditing(false); }} disabled={saving}>取消</button></div>
              </div>
            )}

            <div className="profile-history">
              <div><span>VERSION HISTORY</span><h3>配置历史</h3></div>
              {current.history.length === 0 ? <p>当前为首个配置版本。</p> : (
                <ol>{current.history.map((version, index) => <li key={`${version.writing_profile_id}:${version.writing_profile_version}:${version.created_at ?? index}`}><b>{version.writing_profile_id} · v{version.writing_profile_version}</b><span>从第 {version.writing_profile_effective_from_chapter ?? 1} 章生效</span><small>{version.change_reason || version.created_at || '初始配置'}</small></li>)}</ol>
              )}
            </div>
          </>
        )}
      </section>

      <MemoryPanel projectId={project.project_id} />
    </main>
  );
}
