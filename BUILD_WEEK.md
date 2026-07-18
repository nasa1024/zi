# OpenAI Build Week 2026 — Build Record

This document distinguishes NovelForge's pre-existing foundation from the work completed during the OpenAI Build Week submission period. It also records the Codex/GPT-5.6 evidence behind the submitted release.

## Submission identity

| Field | Value |
|---|---|
| Project | NovelForge |
| Track | Apps for Your Life |
| Repository | <https://github.com/nasa1024/zi> |
| Entrant | lihang |
| Build Week model | `gpt-5.6-sol` |
| Core Codex Session ID | `019f664f-c328-7453-8550-f664e6eead64` |
| Session window | July 15, 2026 08:05–10:54 PDT |
| Pre-Build Week boundary | `ac8dc5ab68cf8d3a62919c389dd5f87bd6c01be2` |
| Build Week feature commit | `f8761e69e16f2525f5c62dd404b45200c3487b34` |

## Pre-existing foundation

Before the submission period, NovelForge already included:

- an append-only Canon fact ledger and proposal-based governance;
- deterministic world-state replay and continuity validators;
- L0/L1/L2 memory, summaries, FTS, and optional vector recall;
- multi-provider generation, budgets, candidate selection, and Autopilot;
- a FastAPI backend, SQLite schema/migrations, and an early web Studio;
- craft checks for pacing, hooks, payoff, voice, and foreshadowing.

This foundation is not presented as Build Week work. Judges should evaluate the extension below.

## Meaningful extension completed during Build Week

The core session transformed an existing collection of planning components into an end-to-end, outline-driven control loop.

### 1. One planning authority

`novelforge/planning/service.py` became the shared service for book, volume, future-window, and chapter-plan decisions. Planning entrances now share validation, transaction boundaries, and version semantics instead of mutating disconnected tables.

### 2. Complete chapter contracts

Chapter plans became explicit contracts covering objective, key beats, required and forbidden facts, character state, arc intent, payoff obligations, and craft targets. Drafting and judging consume the same contract.

### 3. Evidence-backed actuals

The final selected draft—not discarded candidates—produces structured actuals with evidence spans. This prevents rejected text from contaminating the story state or alignment report.

### 4. Drift as a measurable signal

`novelforge/planning/scoring.py` compares contract intent with final actuals using weighted, explainable dimensions. The UI shows both a score and the evidence behind it.

### 5. Safe future replanning

`novelforge/planning/replanner.py` previews or applies a replacement future window transactionally. It creates append-only plan versions and never rewrites Canon or completed chapters.

### 6. Coherent author experience

The Build Week Studio added a writing workspace, story spine, contract inspector, candidate proofs, alignment report, plan-version timeline, and replan dialog. Authors can move from route choice to chapter evidence without dropping into raw database or API tooling.

### 7. Tests and runtime hardening

The extension added focused planning, alignment, API, runtime-gateway, frontend unit, lint, type, and E2E coverage. The repository currently passes 726 backend tests plus the frontend check pipeline.

## Quantitative diff

The functional Build Week commit changed 70 files:

```text
11,669 insertions
   765 deletions
```

Major new files include:

- `novelforge/planning/service.py`
- `novelforge/planning/scoring.py`
- `novelforge/planning/actuals.py`
- `novelforge/planning/replanner.py`
- `novelforge/app/api/planning.py`
- `novelforge/db/migrations/v20.py`
- `web/src/pages/workspace/WritingWorkspacePage.tsx`
- planning and editor evidence components
- three dedicated planning/alignment test modules

Reproduce the boundary locally:

```bash
git diff --stat ac8dc5ab68cf8d3a62919c389dd5f87bd6c01be2 f8761e69e16f2525f5c62dd404b45200c3487b34
```

## How Codex and GPT-5.6 were used

The entrant provided the product requirements, existing architectural constraints, and the decision that future plans must not be treated as Canon. Codex with GPT-5.6 then:

- mapped the existing schema, planning modules, API routes, orchestrator, and UI;
- converted the requirements into a cross-layer implementation plan;
- implemented the schema migration, repositories, service, actuals, scoring, replanning, and API;
- integrated the closed loop into generation and the durable agent runtime;
- designed and implemented the new workspace in the existing visual language;
- added and ran backend, frontend, and build verification;
- iterated on failures until the full suite passed.

The entrant retained the key product and engineering decisions: append-only history, final-only actuals, explicit transaction boundaries, evidence-first drift, deterministic hard-state validation, local-first operation, and a UI that exposes rather than hides agent decisions.

## Commit SHA note

The original Codex session ended by creating commit `f2ab1ee`. A later credential-removal history rewrite changed that commit's SHA to `f8761e6`; its timestamp and file tree remained unchanged. This explains why the local Codex session transcript names the former SHA while the public repository contains the latter.

## Verification

```bash
.venv/bin/python -m pytest -q
# 726 passed

cd web
npm run check
# typecheck + eslint + vitest + production build
```

See [docs/submission/JUDGE_TESTING.md](docs/submission/JUDGE_TESTING.md) for the shortest evaluation route and [docs/submission/DEMO_SCRIPT.md](docs/submission/DEMO_SCRIPT.md) for the video narrative.
