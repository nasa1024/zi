export interface ContentUnitCount {
  value: number;
  unit: 'words' | 'characters';
}

const ENGLISH_WORD = /[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu;

export function countContentUnits(text: string, contentLanguage?: string | null): ContentUnitCount {
  if ((contentLanguage ?? 'zh-CN').toLowerCase().startsWith('en')) {
    return { value: text.match(ENGLISH_WORD)?.length ?? 0, unit: 'words' };
  }
  return { value: text.replace(/\s/gu, '').length, unit: 'characters' };
}
