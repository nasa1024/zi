import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import {
  agentStageLabel,
  alignmentEvidenceLabel,
  arcTypeLabel,
  audienceRatingLabel,
  beatTypeLabel,
  contentLanguageLabel,
  contentUnitLabel,
  factTypeLabel,
  genreLabel,
  governanceModeLabel,
  interactionModeLabel,
  memoryWarningText,
  planActionLabel,
  planReasonLabel,
  planStatusLabel,
  publicationFormatLabel,
  riskTierLabel,
  searchModeLabel,
  targetMarketLabel,
  targetStateKeyLabel,
  targetStateValueLabel,
  userFacingError,
  systemCopyText,
  writingProfileDescription,
  writingProfileLabel,
  writingRuleLabel,
} from './uiText';

describe('uiText', () => {
  it('presents product configuration values in clear Chinese', () => {
    expect(contentLanguageLabel('zh-CN')).toBe('简体中文');
    expect(contentLanguageLabel('en-US', true)).toBe('英语（美国） · en-US');
    expect(targetMarketLabel('global_english')).toBe('全球英语读者');
    expect(publicationFormatLabel('web_serial')).toBe('连载小说');
    expect(audienceRatingLabel('young_adult')).toBe('青少年适读');
    expect(writingRuleLabel('third_person_limited')).toBe('第三人称限知');
    expect(writingProfileLabel('cn_web_novel_v1')).toBe('中文网文通用风格');
    expect(writingProfileLabel('en_progression_serial_v1', 'Progression Fantasy Serial')).toBe('成长奇幻连载');
    expect(writingProfileDescription('en_progression_serial_v1', 'Visible growth.')).toContain('可见成长');
    expect(contentUnitLabel('zh-CN')).toBe('字');
    expect(contentUnitLabel('en-US')).toBe('词');
  });

  it('translates workflow and planning enums without exposing internal values', () => {
    expect(governanceModeLabel('human_gate')).toBe('人工审核');
    expect(interactionModeLabel('copilot')).toBe('协作创作');
    expect(agentStageLabel('draft_choice')).toBe('选择候选正文');
    expect(planStatusLabel('superseded')).toBe('历史保留');
    expect(planActionLabel('keep')).toBe('保持原计划');
    expect(beatTypeLabel('turn')).toBe('转折');
    expect(beatTypeLabel('hook')).toBe('章末钩子');
    expect(beatTypeLabel('tension_point')).toBe('张力升级');
    expect(targetStateKeyLabel('knowledge_edges')).toBe('人物认知');
    expect(riskTierLabel('high')).toBe('高风险');
    expect(genreLabel('young_adult')).toBe('青少年小说');
    expect(genreLabel('future_genre', 'Speculative Fiction')).toBe('推想小说');
    expect(genreLabel('unknown_genre', 'Untranslated Genre')).toBe('自定义类型');
    expect(arcTypeLabel('foreshadow')).toBe('伏笔线');
    expect(factTypeLabel('constraint')).toBe('设定禁忌');
    expect(searchModeLabel('bm25')).toBe('关键词相关度检索');
    expect(targetStateValueLabel('advanced')).toBe('已推进');
    expect(targetStateValueLabel('broken')).toBe('已经破裂');
    expect(targetStateValueLabel('knows')).toBe('已经知道');
    expect(targetStateValueLabel('top_secret')).toBe('最高机密');
    expect(targetStateValueLabel('unmapped_internal_state')).toBe('状态已记录');
    expect(alignmentEvidenceLabel('proposal_evidence')).toBe('设定提案证据');
    expect(planReasonLabel('agent_replan', 'superseded')).toBe('创作助手重新规划');
    expect(systemCopyText('保存 checkpoint，稍后继续。')).toBe('保存当前进度，稍后继续。');
    expect(systemCopyText('Save the checkpoint and continue later.')).toBe('保存当前进度，稍后继续。');
    expect(systemCopyText('Generate Chapter 12')).toBe('生成第 12 章');
    expect(systemCopyText('Chapter 12 is planned. What should happen next?')).toBe(
      '第 12 章已经规划完成，下一步怎么做？',
    );
    expect(systemCopyText('initial profile')).toBe('初始配置');
    expect(systemCopyText('至少完成一项 must_advance')).toBe('至少完成一项本章必须推进的内容');
    expect(systemCopyText('模型裁决失败，已确定性回退：TypeError')).toBe(
      '智能推荐暂不可用：已按候选评分自动选择。',
    );
  });

  it('translates real memory health warnings without exposing storage-layer names', () => {
    expect(memoryWarningText('2 个最新 L0 文件缺失')).toBe('有 2 个最新正文文件缺失');
    expect(memoryWarningText('L0 FTS 尚有 3 章未索引')).toBe('原始正文层的全文检索索引尚有 3 章未完成');
    expect(memoryWarningText('L1 尚有 2 章未完成')).toBe('证据层尚有 2 章未完成提取');
    expect(memoryWarningText('L1 有 4 条原子缺少可核验证据行')).toBe(
      '证据层有 4 条记录缺少可核验的正文依据',
    );
    expect(memoryWarningText('L2 尚有 1 章待聚合')).toBe('场景记忆层尚有 1 章等待整理');
  });

  it('keeps internal routes and English transport failures out of user messages', () => {
    expect(userFacingError(
      new ApiError('not found: /v1/demo/story-map', 404),
      '故事规划加载失败',
    )).toBe('请求的内容暂时不可用，请刷新后重试。');
    expect(userFacingError(new TypeError('Failed to fetch'), '加载失败')).toBe(
      '网络连接失败，请检查服务状态后重试。',
    );
    expect(userFacingError(new Error('temporary shelf failure'), '书架加载失败')).toBe('书架加载失败');
    expect(userFacingError(new Error('写作配置暂时无法保存'), '保存失败')).toBe('写作配置暂时无法保存');
    expect(userFacingError(
      new Error('draft 失败: 所有候选均无产出'),
      '生成失败',
    )).toContain('所有候选稿都没有生成内容');
    expect(userFacingError(new Error('保存 Agent 会话失败'), '保存失败')).toBe('保存失败');
  });
});
