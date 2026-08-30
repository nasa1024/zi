import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import StoryFlowRail from './StoryFlowRail';

describe('StoryFlowRail', () => {
  it('keeps the continue checkpoint current when its link leads to the next chapter', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const markup = renderToStaticMarkup(
      createElement(
        MemoryRouter,
        { initialEntries: ['/studio/project/write/4#continue'] },
        createElement(StoryFlowRail, {
          projectId: 'project',
          currentChapter: 4,
          currentStep: 'continue',
          workspaceSearch: '?session=agent',
        }),
      ),
    );
    consoleError.mockRestore();

    expect(markup).toContain('class="story-flow-link is-current" aria-current="step"');
    expect(markup).toContain('href="/studio/project/write/5?session=agent"');
  });
});
