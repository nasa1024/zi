import { describe, expect, it } from 'vitest';
import { countContentUnits } from './contentCount';

describe('countContentUnits', () => {
  it('counts English words instead of non-whitespace characters', () => {
    expect(countContentUnits("A well-lit hall isn't empty.", 'en-US')).toEqual({
      value: 5,
      unit: 'words',
    });
  });

  it('keeps the existing non-whitespace character count for Chinese projects', () => {
    expect(countContentUnits('天地  初开。', 'zh-CN')).toEqual({
      value: 5,
      unit: 'characters',
    });
  });
});
