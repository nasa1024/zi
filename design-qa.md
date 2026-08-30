# zi AI 小说创作前端：Design QA

## Comparison target

- Source visual truth: `/Users/nasa1024/.codex/generated_images/01a04f55-a5b7-74f2-8ade-9abc9bb78214/exec-6f940fce-f40a-4798-b2dd-50c313c4bc6d.png`
- Normalized source: `docs/zi-reference-normalized-1440x1024.png`
- Final browser-rendered implementation: `docs/zi-redesign-writing-generating-final-1440x1024.png`
- Final full-view comparison: `docs/zi-design-qa-comparison-pass5-final.png`
- Final focused comparisons: `docs/zi-design-qa-focus-top-final.png` and `docs/zi-design-qa-focus-bottom-final.png`
- Final mobile implementation: `docs/zi-redesign-writing-mobile-final-390x844.png`
- Browser route used for controlled visual QA: `http://127.0.0.1:5176/studio/design-qa-book/write/5`
- Actual application entry verified separately: `http://127.0.0.1:5175/studio`

## Normalization and state

- Source pixels: 1487 x 1058. The source was proportionally normalized to 1440 x 1024 before comparison.
- Implementation pixels: 1440 x 1024.
- CSS viewport: 1440 x 1024.
- `deviceScaleFactor`: 1; browser-reported `devicePixelRatio`: 1.
- Comparison density: 1:1 after normalization.
- State: chapter 5, AI generation at 62%, previous candidates retained as read-only, chapter contract visible.
- Full-view evidence is a real 2880 x 1024 side-by-side image containing the normalized source and browser-rendered implementation, not two separately judged images.
- Focused evidence uses 2880 x 420 top and 2880 x 504 bottom side-by-side crops so navigation, typography, manuscript, candidates, and contract details remain readable.

## Findings

- No actionable P0, P1, or P2 mismatch remains.
- The implementation intentionally adapts the reference rather than copying its information model: the left grouped timeline becomes a chapter-level Story Spine, the right context/notes rail becomes the chapter contract and divergence check, and two compact candidate snippets become a three-candidate proofing table. These changes support the optimized seven-step author flow while retaining the selected direction's warm-paper palette, neo-brutalist borders and shadows, bright lime/pink/blue accents, three-column writing composition, and clear current-step emphasis.
- [P3] On the 390 x 844 mobile viewport, the persistent bottom workspace navigation can cover a small part of long manuscript text while scrolling. Core controls remain reachable and there is no horizontal overflow; this is a follow-up polish item, not a release blocker.

## Required fidelity surfaces

- Fonts and typography: the final build uses the product's Syne/PingFang/Hiragino/Noto fallback stack, with optical weight separated between display numerals, Chinese titles, body copy, and mono metadata. The chapter title, step labels, body copy, small state labels, wrapping, and truncation were checked in both full and focused comparisons. Small pink and blue text now uses higher-contrast tokens.
- Spacing and layout rhythm: the 1440 x 1024 frame keeps the seven-step rail above a three-column writing layout. Story Spine, manuscript, generation rail, proofing table, and contract rail align to a shared rhythm. During generation the read-only manuscript is compressed so the candidate decision surface remains in the first viewport.
- Colors and visual tokens: warm paper, near-black ink, lime current state, pink action/generation state, blue volume card, orange warning, hard borders, and offset shadows follow the source direction. Semantic state colors remain distinguishable and small colored text has strengthened contrast.
- Image quality and asset fidelity: the screen contains no photographic or illustrative content requiring generated raster assets. The existing zi brand mark and product icon language were retained as established product-system elements. Browser captures are sharp at density 1 with no scaling blur, masking halo, or compression artifact that affects judgment.
- Copy and content: labels describe author decisions rather than backend mechanics: `开始 -> 定方向 -> 排未来 -> 写本章 -> 选稿 -> 校对 -> 继续`. Generation, saved/dirty, historical candidates, stop-waiting, reconciliation, chapter contract, and divergence copy all match their actual state.

## Comparison history

1. Pass 1 — `docs/zi-design-qa-comparison-pass1.png`
   - Earlier P2: the manuscript consumed almost the entire center viewport, leaving the candidate decision surface below the fold even though candidate selection is the next action.
   - Fix: reduced and rebalanced manuscript height, tightened center-column vertical rhythm, and promoted the proofing table.
   - Post-fix evidence: pass 2 shows the candidate cards entering the first viewport.
2. Pass 2 — `docs/zi-design-qa-comparison-pass2.png`
   - The earlier P2 was resolved. Remaining differences were P3 density and typography refinements only.
   - Fix: tuned title scale, column proportions, metadata spacing, candidate-card density, and contract-rail hierarchy.
   - Post-fix evidence: pass 3 preserves first-viewport decisions with a calmer type and spacing hierarchy.
3. Pass 3 — `docs/zi-design-qa-comparison-pass3.png`
   - No new P0/P1/P2 in the settled writing state.
   - Next check: reproduce the source's active generation state rather than accepting a settled-state comparison.
4. Pass 4 — `docs/zi-design-qa-comparison-pass4-generating.png`
   - Earlier P2: adding the real 62% generation rail pushed all candidates out of the first viewport, weakening continuity from generation to selection.
   - Fix: apply a compact read-only manuscript treatment only while generation/reconciliation is active, keep prior candidates visible, and make them explicitly read-only until the new run settles.
   - Post-fix evidence: pass 5 restores the candidate proofing table to the first viewport while retaining progress, ETA, and stop-waiting semantics.
5. Pass 5 — `docs/zi-design-qa-comparison-pass5-final.png`
   - Final full-view and focused comparisons show no actionable P0/P1/P2 differences. The optimized layout is visually coherent with the source direction and keeps the next author decision visible.

## Interaction and responsive verification

- Bookshelf recommends the concrete next action and fails closed when checkpoint/session data is unknown or errors.
- The rolling planning window starts from the next chapter and updates with project progress.
- The seven-step rail, main navigation, chapter navigation, and continuation links preserve the `session` query when present.
- Chapter generation exposes progress and ETA; `停止等待` stops only the page wait, keeps duplicate generation locked, and reconciles the exact backend run to a terminal state.
- Refresh recovery resumes reconciliation of a non-terminal run; a new generation keeps old candidates visible but read-only; failed generation restores candidate selection.
- Candidate comparison uses three drafts, separates contract/arc ratios from 0-10 prose-quality scores, prevents historical candidates from overwriting newer manuscript revisions, and advances to proofread after adoption.
- Dirty manuscript, active generation/reconciliation, and candidate-sync states block unsafe navigation. In the in-app browser, editing the chapter then choosing World opened the protection dialog; `留在本章` was verified as `document.activeElement`, and the test edit was discarded without saving.
- Mobile was checked at 390 x 844: the flow rail occupies y=70-129, Story Spine begins at y=143, the current-step center offset is -0.484 px, and the two regions do not overlap.
- Browser error logs were checked after final QA on both the controlled QA tab and actual application tab: 0 error-level entries. The controlled QA tab was restored to a clean chapter 5 writing state at 1440 x 1024.

## Automated verification

- Typecheck: passed.
- ESLint: passed with 0 warnings.
- Vitest: 5 files, 11/11 tests passed.
- Production build: passed; 84 modules transformed with no bundle/chunk warning.
- Playwright Chromium E2E: 22/22 tests passed.
- `git diff --check`: passed.

## Implementation checklist

- [x] Optimized seven-step writing journey is visible and navigable.
- [x] Direction, planning, writing, candidate, proofread, and continue states are wired to real UI behavior.
- [x] Desktop source/implementation comparison is normalized and includes full-view plus focused evidence.
- [x] P0/P1/P2 comparison findings were fixed and visually rechecked.
- [x] Mobile layout and current-step centering were measured.
- [x] Primary interactions, unsafe-navigation focus, browser errors, unit tests, build, and E2E were verified.

## Follow-up polish

- P3: add a little more mobile bottom safe-area/padding so long manuscript text never sits behind the persistent workspace navigation.

final result: passed
