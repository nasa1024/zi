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
import {
  audienceRatingLabel,
  contentLanguageLabel,
  contentWarningLabel,
  genreLabel,
  publicationFormatLabel,
  storyTagLabel,
  systemCopyText,
  targetMarketLabel,
  userFacingError,
  writingProfileDescription,
  writingProfileLabel,
  writingRuleLabel,
} from '../../utils/uiText';

function message(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}

function splitList(value: string): string[] {
  return Array.from(new Set(value.split(/[,，]/).map((item) => item.trim()).filter(Boolean)));
}

const KNOWN_STORY_TAG_VALUES = [
  'progression_fantasy',
  'litrpg',
  'cozy',
  'cultivation',
  'system',
  'romance',
  'mystery',
  'action',
];
const KNOWN_CONTENT_WARNING_VALUES = [
  'graphic_violence',
  'profanity',
  'sexual_content',
  'self_harm',
];

function catalogGenreLabel(genre: GenreCatalogItem): string {
  const mapped = genreLabel(genre.id);
  return mapped === '自定义类型' ? genreLabel(genre.id, genre.label) : mapped;
}

function profileLabel(identifier: unknown, displayName?: string | null): string {
  const mapped = writingProfileLabel(identifier);
  return mapped === '自定义写作风格'
    ? writingProfileLabel(identifier, displayName)
    : mapped;
}

function mappedListText(values: string[], labeler: (value: unknown) => string): string {
  return values.map(labeler).join(', ');
}

function parseMappedList(
  value: string,
  knownValues: string[],
  existingValues: string[],
  labeler: (item: unknown) => string,
): string[] {
  const valuesByLabel = new Map<string, string[]>();
  [...knownValues, ...existingValues].forEach((item) => {
    const label = labeler(item);
    valuesByLabel.set(label, [...(valuesByLabel.get(label) ?? []), item]);
  });
  return Array.from(new Set(
    splitList(value).flatMap((item) => valuesByLabel.get(item) ?? [item]),
  ));
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
    setStoryTagsText(mappedListText(profile.story_tags, storyTagLabel));
    setAudienceRating(profile.audience_rating ?? 'general');
    setContentWarningsText(mappedListText(profile.content_warnings, contentWarningLabel));
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
      story_tags: parseMappedList(
        storyTagsText,
        KNOWN_STORY_TAG_VALUES,
        current.story_tags,
        storyTagLabel,
      ),
      audience_rating: audienceRating,
      content_warnings: parseMappedList(
        contentWarningsText,
        KNOWN_CONTENT_WARNING_VALUES,
        current.content_warnings,
        contentWarningLabel,
      ),
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
        setSavedMessage(`写作配置第 ${updated.writing_profile_version} 版已保存，从第 ${updated.writing_profile_effective_from_chapter ?? effectiveFromChapter} 章生效。`);
        refetchProjects();
      };
      await commit(false);
    } catch (saveError) {
      if (
        saveError instanceof ApiError
        && saveError.code === 'high_risk_confirmation_required'
        && window.confirm('这次修改会改变叙事视角或时态，只能从下一章起生效。仍要保存新版本吗？')
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
          setSavedMessage(`写作配置第 ${updated.writing_profile_version} 版已保存，从第 ${updated.writing_profile_effective_from_chapter ?? effectiveFromChapter} 章生效。`);
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
        <div><span>项目配置 · 仅影响本作品</span><h1>项目设置</h1></div>
        <p>写作配置按版本保存，只影响生效章节之后的生成；已有正文与已确认设定不会被改写。</p>
      </header>

      <section className="writing-profile-panel">
        <div className="panel-head">
          <div><span>语言 · 市场 · 风格</span><h2>写作配置</h2><p>这里显示当前采用的配置；修改后只影响未来章节。</p></div>
          {!loading && current && !editing && <button type="button" className="nf-btn" onClick={() => { setEditing(true); setSavedMessage(null); }}>修改未来章节配置</button>}
        </div>

        {error && <div className="nf-msg err" role="alert"><span>⚠</span><span>{error}</span><button type="button" className="nf-btn-sm" onClick={() => void load()}>重试</button></div>}
        {savedMessage && <div className="nf-msg"><span>✓</span><span>{savedMessage}</span></div>}
        {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在读取写作配置…</div> : current && (
          <>
            <div className="profile-current-card">
              <div><span>作品语言</span><b>{contentLanguageLabel(current.content_language, true)}</b><small>{canEditLanguage ? '尚无正文，可直接修改作品语言' : '已有正文；当前版本暂不支持自动迁移作品语言'}</small></div>
              <div><span>目标市场</span><b>{targetMarketLabel(current.target_market)}</b><small>{publicationFormatLabel(current.publication_format)}</small></div>
              <div><span>写作风格</span><b>{profileLabel(current.writing_profile_id, current.profile?.name)}</b><small>第 {current.writing_profile_version} 版</small></div>
              <div><span>生效章节</span><b>第 {current.writing_profile_effective_from_chapter ?? 1} 章</b><small>{genreLabel(current.primary_genre || project.genre)}</small></div>
            </div>

            {editing && (
              <div className="profile-settings-form">
                <div className="nf-field"><label htmlFor="settings-language">作品语言</label><select id="settings-language" className="nf-select" value={contentLanguage} onChange={(event) => handleContentLanguageChange(event.target.value)} disabled={saving || !canEditLanguage}><option value="zh-CN">{contentLanguageLabel('zh-CN', true)}</option><option value="en-US">{contentLanguageLabel('en-US', true)}</option></select></div>
                <div className="nf-field"><label htmlFor="settings-market">目标市场</label><select id="settings-market" className="nf-select" value={targetMarket} onChange={(event) => setTargetMarket(event.target.value)} disabled={saving}>{contentLanguage.startsWith('en') ? <option value="global_english">{targetMarketLabel('global_english')}</option> : <option value="cn_web">{targetMarketLabel('cn_web')}</option>}</select></div>
                <div className="nf-field"><label htmlFor="settings-format">发布形态</label><select id="settings-format" className="nf-select" value={publicationFormat} onChange={(event) => setPublicationFormat(event.target.value)} disabled={saving}><option value="web_serial">{publicationFormatLabel('web_serial')}</option><option value="novel" disabled>{publicationFormatLabel('novel')}（暂未开放）</option></select></div>
                <div className="nf-field"><label htmlFor="settings-genre">主类型</label><select id="settings-genre" className="nf-select" value={primaryGenre} onChange={(event) => setPrimaryGenre(event.target.value)} disabled={saving}>{genres.map((genre) => <option key={genre.id} value={genre.id}>{catalogGenreLabel(genre)}</option>)}</select></div>
                <div className="nf-field"><label htmlFor="settings-profile">写作风格</label><select id="settings-profile" className="nf-select" value={profileId} onChange={(event) => { setProfileId(event.target.value); setProfileOverrides({}); }} disabled={saving}>{profiles.map((profile) => <option key={`${profile.id}:${profile.version}`} value={profile.id}>{profileLabel(profile.id, profile.name)} · 第 {profile.version} 版</option>)}</select></div>
                <div className="nf-field full"><label htmlFor="settings-tags">子类型 / 标签</label><input id="settings-tags" className="nf-input" value={storyTagsText} onChange={(event) => setStoryTagsText(event.target.value)} placeholder="例如：成长奇幻、游戏化冒险" disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-rating">读者分级</label><select id="settings-rating" className="nf-select" value={audienceRating} onChange={(event) => setAudienceRating(event.target.value)} disabled={saving}><option value="general">{audienceRatingLabel('general')}</option><option value="young_adult">{audienceRatingLabel('young_adult')}</option><option value="adult">{audienceRatingLabel('adult')}</option></select></div>
                <div className="nf-field"><label htmlFor="settings-warnings">内容警告<span className="setting-optional">（逗号分隔）</span></label><input id="settings-warnings" className="nf-input" value={contentWarningsText} onChange={(event) => setContentWarningsText(event.target.value)} disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-effective">生效章节</label><input id="settings-effective" className="nf-input" type="number" min={project.chapter_count + 1} value={effectiveFromChapter} onChange={(event) => setEffectiveFromChapter(Math.max(project.chapter_count + 1, Number(event.target.value) || project.chapter_count + 1))} disabled={saving} /></div>
                <div className="nf-field"><label htmlFor="settings-reason">变更原因<span className="setting-optional">（选填）</span></label><input id="settings-reason" className="nf-input" value={changeReason} onChange={(event) => setChangeReason(event.target.value)} disabled={saving} /></div>
                {selectedProfile && <div className="profile-settings-preview full"><b>{profileLabel(selectedProfile.id, selectedProfile.name)}</b><p>{writingProfileDescription(selectedProfile.id, selectedProfile.description || selectedProfile.preview)}</p><small>{writingRuleLabel(resolvedRules.pov)} · {writingRuleLabel(resolvedRules.tense)} · {writingRuleLabel(resolvedRules.pace)}</small></div>}
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
                      <option value="">使用预设（{writingRuleLabel(selectedProfile?.prompt_rules[key as string])}）</option>
                      {(values as string[]).map((value) => <option key={value} value={value}>{writingRuleLabel(value)}</option>)}
                    </select>
                  </div>
                ))}
                <div className="nf-actions full"><button type="button" className="nf-btn pink" onClick={() => void save()} disabled={saving || !profileId}>{saving ? '保存中…' : '保存新版本'}</button><button type="button" className="nf-btn ghost" onClick={() => { applyCurrent(current); setEditing(false); }} disabled={saving}>取消</button></div>
              </div>
            )}

            <div className="profile-history">
              <div><span>配置历史</span><h3>历史版本</h3></div>
              {current.history.length === 0 ? <p>当前为首个配置版本。</p> : (
                <ol>{current.history.map((version, index) => <li key={`${version.writing_profile_id}:${version.writing_profile_version}:${version.created_at ?? index}`}><b>{profileLabel(version.writing_profile_id)} · 第 {version.writing_profile_version} 版</b><span>从第 {version.writing_profile_effective_from_chapter ?? 1} 章生效</span><small>{version.change_reason ? systemCopyText(version.change_reason, '初始配置') : (version.created_at || '初始配置')}</small></li>)}</ol>
              )}
            </div>
          </>
        )}
      </section>

      <MemoryPanel projectId={project.project_id} />
    </main>
  );
}
