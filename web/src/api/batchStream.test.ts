import { describe, expect, it } from 'vitest';
import { dispatchBatchEvent, parseSSEBuffer } from './client';
import type { BatchStreamHandlers } from './types';

describe('multi-chapter batch SSE parsing (v22/F6)', () => {
  it('parses complete data lines and keeps the partial tail', () => {
    const chunk =
      'data: {"event":"batch_start","batch_id":"b1","mode":"batch","chapters":[3,4],"capacity":10}\n\n'
      + ': ping\n\n'
      + 'data: {"event":"stage","chapter":3,"stage":"recall","status":"ok","detail":{}}\r\n\n'
      + 'data: not-json\n\n'
      + 'data: {"event":"chapter_done","chap';
    const first = parseSSEBuffer(chunk);
    expect(first.events).toEqual([
      { event: 'batch_start', batch_id: 'b1', mode: 'batch', chapters: [3, 4], capacity: 10 },
      { event: 'stage', chapter: 3, stage: 'recall', status: 'ok', detail: {} },
    ]);
    expect(first.rest).toBe('data: {"event":"chapter_done","chap');

    const second = parseSSEBuffer(first.rest + 'ter":3,"ok":true}\n\n');
    expect(second.events).toEqual([{ event: 'chapter_done', chapter: 3, ok: true }]);
    expect(second.rest).toBe('');
  });

  it('routes every batch event type to its handler', () => {
    const seen: string[] = [];
    const handlers: BatchStreamHandlers = {
      onStart: (e) => seen.push(`start:${e.mode}`),
      onDraft: (e) => seen.push(`draft:${e.from_chapter}-${e.to_chapter}`),
      onStage: (e) => seen.push(`stage:${e.chapter}:${e.stage}`),
      onChapterDone: (e) => seen.push(`done:${e.chapter}:${e.ok}`),
      onDiscarded: (e) => seen.push(`discarded:${e.chapters.join(',')}`),
      onRegenerate: (e) => seen.push(`regen:${e.from_chapter}#${e.attempt}`),
      onDone: (e) => seen.push(`batch_done:${e.status}`),
      onError: (e) => seen.push(`error:${e.message}`),
    };
    const events: unknown[] = [
      { event: 'batch_start', mode: 'batch', chapters: [1, 2, 3], capacity: 10 },
      { event: 'batch_draft', batch_id: 'b1', from_chapter: 1, to_chapter: 3, parsed: [1, 3],
        rejected: { 2: 'too_short' }, tokens: 10, usd: 0.1 },
      { event: 'stage', chapter: 1, stage: 'check', status: 'ok', detail: {} },
      { event: 'chapter_done', chapter: 1, ok: true },
      { event: 'batch_discarded', batch_id: 'b1', from_chapter: 2, chapters: [2, 3] },
      { event: 'batch_regenerate', batch_id: 'b2', from_chapter: 2, attempt: 1 },
      { event: 'batch_done', status: 'completed' },
      { event: 'error', message: 'boom' },
      { event: 'done' },        // 单章事件不属于连写流：忽略
      null,
      'garbage',
    ];
    for (const ev of events) dispatchBatchEvent(ev, handlers);
    expect(seen).toEqual([
      'start:batch',
      'draft:1-3',
      'stage:1:check',
      'done:1:true',
      'discarded:2,3',
      'regen:2#1',
      'batch_done:completed',
      'error:boom',
    ]);
  });
});
