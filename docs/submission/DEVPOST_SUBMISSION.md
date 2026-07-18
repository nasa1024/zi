# Devpost Submission Copy

This file is ready to adapt into the OpenAI Build Week submission form. Replace the bracketed YouTube URL after publishing the demo.

## Manual steps before submission

- [ ] Join the challenge with the same identity used for the submission.
- [ ] Open the Codex task titled **Implement NovelForge outline transformation** / **实现 NovelForge 大纲改造**, run `/feedback`, and confirm the returned Session ID is `019f664f-c328-7453-8550-f664e6eead64`.
- [ ] Record the product using [DEMO_SCRIPT.md](DEMO_SCRIPT.md), upload it publicly to YouTube, and replace the placeholder below.
- [ ] Paste the English copy from this file into Devpost and select **Apps for Your Life**.
- [ ] Use [JUDGE_TESTING.md](JUDGE_TESTING.md) as the testing instructions.
- [ ] Confirm the repository is public, the MIT license is visible, CI is green, and no real credentials exist in local or Git history.
- [ ] Submit before July 21, 2026 at 5:00 PM PDT.

## Project name

NovelForge

## Tagline

An AI writing engine that remembers every choice—and turns it into a better next chapter.

## Track

Apps for Your Life

## Repository

<https://github.com/nasa1024/zi>

## Demo video

`[PUBLIC YOUTUBE URL — add before submission]`

## Codex Session ID

`019f664f-c328-7453-8550-f664e6eead64`

## Short description

NovelForge is local-first infrastructure for long-form fiction. It combines hierarchical memory, deterministic continuity, narrative craft, and an outline-to-actuals feedback loop so an AI can write many chapters without quietly forgetting the world, breaking character state, or drifting away from the author's intended story.

## Inspiration

Most AI writing products are optimized for a good next paragraph. A novelist needs something harder: a system that can remember decisions across a book, distinguish hard facts from soft context, preserve future intent without pretending the future already happened, and explain why a generated chapter has drifted.

The central insight behind NovelForge is that long-form writing is not one prompt. It is a state-management and feedback-control problem wrapped in a creative experience.

## What it does

NovelForge gives an author a complete writing loop:

1. Choose or generate a future story route.
2. Turn the next chapter into an explicit contract.
3. Recall relevant facts, scenes, and world state.
4. Generate and compare multiple candidate drafts.
5. Block deterministic continuity failures and revise softer issues.
6. Promote only reviewed fact proposals into an append-only Canon.
7. Extract what actually happened from the final draft.
8. Score drift against the contract and preview a safe future replan.

The Studio exposes the contract, candidates, evidence, plan versions, world state, memory health, and review queue. The deterministic core runs locally without an external model key.

## How we built it

NovelForge uses Python 3.13, FastAPI, SQLite, React, TypeScript, and Vite. Hard story state lives in relational tables and is replayed at a chapter boundary. Soft context uses layered memory and hybrid retrieval. Models can propose changes, but only a single governance gate can promote them into Canon.

The Build Week extension used Codex with GPT-5.6 to close the outline-driven loop across 70 files. It added a unified planning service, versioned chapter contracts, evidence-backed actuals, weighted drift scoring, transactional future replanning, runtime capability negotiation, a complete writing workspace, and focused backend/frontend tests.

## How we collaborated with Codex and GPT-5.6

I supplied the product requirements and architectural invariants: Canon must be append-only; future plans are hypotheses; actuals come only from final prose; hard continuity is deterministic; and authors must see evidence behind agent decisions.

Codex used GPT-5.6 to map the existing repository, plan the cross-layer change, implement the database/API/runtime/UI work, add tests, diagnose failures, and iterate until the complete suite passed. The primary build session is `019f664f-c328-7453-8550-f664e6eead64`.

Because NovelForge existed before Build Week, the repository includes a transparent boundary document. The evaluated extension is commit `f8761e6`, created during the submission period, with 11,669 additions and 765 deletions. Pre-existing components are explicitly excluded from the claimed Build Week work.

## Challenges

- Keeping plans versioned without accidentally turning possible futures into Canon.
- Ensuring rejected candidates never contaminate actuals or memory.
- Defining drift dimensions that are explainable to an author rather than producing one opaque score.
- Applying a multi-chapter replan atomically while preserving completed work.
- Showing complex agent state in a writing experience that still feels calm and creative.

## Accomplishments

- A complete outline → contract → draft → actuals → drift → replan loop.
- Deterministic continuity checks with traceable evidence.
- Append-only Canon and plan histories that support audit and recovery.
- A coherent Studio rather than a collection of backend endpoints.
- 726 passing backend tests plus frontend types, lint, tests, and production build.
- A useful offline path that lets judges explore the core without providing credentials.

## What we learned

The most useful role for an AI agent in creative software is not hiding every decision. It is making more decisions possible while keeping state, evidence, and reversibility visible to the human author.

Codex was especially effective when the work crossed schema, service, API, runtime, UI, and tests. The human contribution that mattered most was specifying invariants and product taste; the agent accelerated implementation and exhaustive integration.

## What's next

- Visual causality graphs for character knowledge and event dependencies.
- Author-controlled evaluation profiles by genre.
- Collaborative projects with branch-aware review.
- A hosted zero-install demo and import/export adapters for common writing formats.
- Longitudinal evaluation across full novels, not isolated chapter benchmarks.

## Testing instructions

Follow [docs/submission/JUDGE_TESTING.md](JUDGE_TESTING.md). The no-key path takes approximately five minutes after dependency installation and includes project creation, world-state inspection, planning, review, and memory health.
