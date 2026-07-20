import { describe, expect, it } from 'vitest';
import {
  normalizeAlignmentReport,
  normalizeChapterContract,
  normalizeProjectWritingProfile,
  normalizePlanningWindow,
  normalizeWritingProfileCatalog,
} from './client';

describe('API response normalizers', () => {
  it('normalizes the versioned writing-profile catalog envelope', () => {
    const profiles = normalizeWritingProfileCatalog({
      catalog_version: '2026-07-21',
      items: [{
        id: 'en_progression_serial_v1',
        version: '1.0.0',
        display_name: 'Progression Fantasy Serial',
        description: 'Visible progress with forward-driving chapter endings.',
        language: 'en-US',
        market: 'global_english',
        publication_format: 'web_serial',
        compatible_genres: ['fantasy'],
        default_story_tags: ['progression_fantasy'],
        defaults: {
          pov: 'third_person_limited',
          tense: 'past',
          pace: 'fast',
          chapter_word_range: [1500, 2500],
        },
      }],
    });

    expect(profiles[0]).toMatchObject({
      id: 'en_progression_serial_v1',
      name: 'Progression Fantasy Serial',
      format: 'web_serial',
      default_story_tags: ['progression_fantasy'],
      prompt_rules: { pov: 'third_person_limited', tense: 'past', pace: 'fast' },
      quality_rules: { chapter_word_range: [1500, 2500] },
    });
  });

  it('unwraps current writing profile and preserves its version history', () => {
    const profile = normalizeProjectWritingProfile({
      current: {
        content_language: 'en-US',
        target_market: 'global_english',
        publication_format: 'web_serial',
        primary_genre: 'fantasy',
        story_tags: ['progression_fantasy'],
        audience_rating: 'general',
        content_warnings: [],
        writing_profile_id: 'en_progression_serial_v1',
        writing_profile_version: 2,
        writing_profile_catalog_version: '1.1.0',
        writing_profile_overrides: { pace: 'balanced' },
        effective_from_chapter: 8,
        display_name: 'Progression Fantasy Serial',
        description: 'Visible progress and forward-driving endings.',
        prompt_rules: { pace: 'balanced' },
        quality_rules: { chapter_word_range: [1500, 2500] },
      },
      history: [{
        content_language: 'en-US',
        target_market: 'global_english',
        publication_format: 'web_serial',
        revision: 1,
        profile_id: 'en_progression_serial_v1',
        overrides: {},
        effective_from_chapter: 1,
      }],
    });

    expect(profile).toMatchObject({
      writing_profile_id: 'en_progression_serial_v1',
      writing_profile_version: 2,
      writing_profile_effective_from_chapter: 8,
      profile: { name: 'Progression Fantasy Serial' },
    });
    expect(profile.history).toHaveLength(1);
    expect(profile.history[0].writing_profile_effective_from_chapter).toBe(1);
  });

  it('uses durable decision options when normalizing a planning window', () => {
    const normalized = normalizePlanningWindow({
      start_chapter: 4,
      horizon: 2,
      candidates: [
        {
          title: 'id-less generator draft',
          payload: { chapters: [{ chapter: 4, goal: '不应被采用' }] },
        },
      ],
      decision: {
        decision_id: 'dec-route',
        recommended_option_id: 'opt-b',
        options: [
          {
            id: 'opt-a',
            title: '稳健推进',
            summary: '先兑现本卷目标。',
            score: 0.82,
            payload: {
              chapters: [
                { chapter: 4, title: '入城', goal: '找到线人', beats: [] },
                { chapter: 5, title: '暗号', goal: '确认内应', beats: [] },
              ],
            },
          },
          {
            id: 'opt-b',
            title: '冒险推进',
            summary: '提前触发冲突。',
            payload: {
              chapters: [
                { chapter: 4, title: '伏击', goal: '逃出包围', beats: [] },
                { chapter: 5, title: '反追踪', goal: '找出叛徒', beats: [] },
              ],
            },
          },
        ],
      },
    });

    expect(normalized).toMatchObject({
      decision_id: 'dec-route',
      recommended_option_id: 'opt-b',
      start_chapter: 4,
      horizon: 2,
    });
    expect(normalized.candidates.map((candidate) => candidate.id)).toEqual(['opt-a', 'opt-b']);
    expect(normalized.candidates[0].chapters[0]).toMatchObject({
      chapter: 4,
      title: '入城',
      status: 'candidate',
    });
  });

  it('flattens a chapter-contract envelope without losing story anchors', () => {
    const normalized = normalizeChapterContract({
      chapter: 7,
      contract: {
        chapter: 7,
        title: '雨夜回信',
        goal: '确认导师失踪与密令有关',
        locked: 1,
        must_advance: ['导师失踪线'],
        must_not_reveal: ['王都内应身份'],
        target_state: { clue_count: 2 },
        beats: [{ seq: 1, beat_type: 'setup', summary: '收到残缺密信' }],
      },
      story_plan: {
        premise: '少年追查被抹去的王朝历史。',
        ending_direction: '公开真相但拒绝登基。',
      },
      current_volume: {
        volume_no: 2,
        title: '雾都旧案',
        objective: '找到第一份王朝原档',
        key_results: ['锁定档案馆入口'],
      },
      plan_version: { version: 4 },
    });

    expect(normalized).toMatchObject({
      chapter: 7,
      premise: '少年追查被抹去的王朝历史。',
      ending_direction: '公开真相但拒绝登基。',
      plan_version: 4,
      must_advance: ['导师失踪线'],
      must_not_reveal: ['王都内应身份'],
    });
    expect(normalized.volume?.key_results?.[0]).toMatchObject({
      text: '锁定档案馆入口',
      status: 'pending',
    });
  });

  it('maps persisted alignment evidence into the inspector shape', () => {
    const normalized = normalizeAlignmentReport({
      chapter: 7,
      goal_score: 0.5,
      beat_score: 0.4,
      arc_score: 0.8,
      volume_score: 0.6,
      reveal_score: 0,
      divergence_score: 0.52,
      divergence_level: 'severe',
      decision: 'pause',
      detail: { hard_reveal: true },
      actuals: {
        id: 'actual-7',
        payload: {
          chapter: 7,
          events: [],
          state_changes: [],
          completed_beats: [],
          arc_progress: [],
          revealed_information: [],
          unresolved_promises: [],
          involved_entities: [],
          evidence_spans: [
            { start_char: 12, end_char: 16, text: '内应身份', label: 'forbidden_reveal' },
          ],
        },
      },
    });

    expect(normalized).toMatchObject({
      level: 'severe',
      recommended_plan_action: 'pause',
      hard_reveal: true,
    });
    expect(normalized.evidence_spans).toEqual([
      {
        start: 12,
        end: 16,
        quote: '内应身份',
        label: 'forbidden_reveal',
        confidence: null,
      },
    ]);
  });
});
