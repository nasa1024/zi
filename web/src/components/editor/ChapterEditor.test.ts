import { createElement, createRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ChapterEditor from './ChapterEditor';

describe('ChapterEditor', () => {
  it('shows an unknown target instead of inventing a word count', () => {
    const markup = renderToStaticMarkup(createElement(ChapterEditor, {
      chapter: 4,
      title: '月台尽头',
      text: '已有正文',
      textareaRef: createRef<HTMLTextAreaElement>(),
      dirty: false,
      canSave: true,
      saving: false,
      saveMessage: '正文已与服务端同步',
      generating: false,
      generationStages: [],
      contentLanguage: 'zh-CN',
      onChange: () => undefined,
      onSave: () => undefined,
      onGenerate: () => undefined,
    }));

    expect(markup).toContain('<span>目标字数</span><strong>—</strong>');
  });
});
