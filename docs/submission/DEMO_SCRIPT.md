# Build Week Demo Video Script

Target length: **2:45–2:55**. The final video must be public on YouTube, include spoken English, show the working product, and contain no copyrighted music. Record at 1440p or 1080p with the browser zoomed so evidence labels remain readable.

## Recording setup

- Start the API at `127.0.0.1:8787` and Studio at `127.0.0.1:5173`.
- Create one sample project before recording.
- Open the project planning workspace in one tab and the writing workspace in another.
- Prepare a contract, candidate proofs, and an alignment result so the video never waits on a live model.
- Keep the GitHub `BUILD_WEEK.md` page open for the final evidence shot.

### Evidence card to show on screen

```text
OpenAI Build Week 2026 · Apps for Your Life
Codex model: gpt-5.6-sol
Core Session ID: 019f664f-c328-7453-8550-f664e6eead64
Build Week commit: f8761e6
70 files · 11,669 additions · 726 tests passed
```

## Script and shot list

### 0:00–0:15 — Hook

**Screen:** zi landing hero, then cut to the writing workspace.

**Voiceover:**

> AI can write a convincing paragraph. A novel is harder. Fifty chapters later, it must remember who knows what, which promises are still open, and whether the story is still going where the author intended. This is zi.

### 0:15–0:38 — The product idea

**Screen:** World workspace, Canon facts, and state projection.

**Voiceover:**

> zi separates long-form writing into three systems: memory for what matters, deterministic continuity for what cannot be wrong, and narrative craft for what makes readers continue. Hard state is replayed from an append-only Canon. The model can propose a change, but it can never silently rewrite the world.

### 0:38–1:05 — Planning and chapter contract

**Screen:** Plan workspace, future route candidates, then contract inspector.

**Voiceover:**

> During Build Week, I used Codex with GPT-5.6 to close the outline-driven loop. An author can choose a future route, then turn the next chapter into an explicit contract: objective, beats, required facts, forbidden outcomes, character state, and payoff obligations.

### 1:05–1:28 — Candidate generation and continuity

**Screen:** Candidate proofs with scores and a blocked candidate.

**Voiceover:**

> zi can generate multiple candidates, compare contract adherence, arc progress, prose quality, and cost, then block hard continuity failures with deterministic Python validators. Soft problems can be patched locally or escalated to a stronger model.

### 1:28–1:55 — Actuals, evidence, and replan

**Screen:** Alignment report, evidence spans, plan-version timeline, replan preview.

**Voiceover:**

> After the author selects the final draft, the system extracts what actually happened with evidence from that draft only. It measures drift against the contract and can preview a transactional replan for future chapters. Completed prose and Canon remain untouched.

### 1:55–2:20 — Codex collaboration

**Screen:** `BUILD_WEEK.md`, highlight session ID, commit, and quantitative diff; briefly show tests passing.

**Voiceover:**

> The project existed before the event, so the repository documents the boundary transparently. The Build Week extension changed seventy files and added over eleven thousand lines. Codex GPT-5.6 mapped the existing architecture, implemented the schema, services, API, runtime and Studio, added tests, and iterated until all seven hundred twenty-six backend tests and the frontend pipeline passed.

### 2:20–2:43 — Impact

**Screen:** Return to the editor and World Bible.

**Voiceover:**

> zi is for serious long-form creators who need more than autocomplete. It runs locally, works across model providers, preserves evidence, and keeps the author in control of every irreversible story decision.

### 2:43–2:52 — Close

**Screen:** Product logo and tagline.

**Voiceover:**

> Consistency prevents disappointment. Narrative momentum earns the next chapter. zi helps an AI do both.

## Final upload checklist

- Duration is under 3:00.
- Video is public, not unlisted if the form requires public visibility.
- Spoken audio explicitly covers the product, Codex, and GPT-5.6.
- No copyrighted music, stock clips, or unlicensed third-party assets.
- Repository URL and project name appear in the YouTube description.
- Captions are enabled and manually checked for technical terms.
