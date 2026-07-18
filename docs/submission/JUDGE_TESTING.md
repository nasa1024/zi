# Judge Testing Guide

NovelForge is a local-first web application. The deterministic product core can be evaluated without an external model account or API key.

## Supported environment

- macOS or Linux
- Python 3.13
- Node.js 20+
- A modern Chromium, Firefox, or Safari browser

Windows users can run the Python and Node commands in PowerShell; `run-backend.ps1` is also included for loading a local `.env` when testing live generation.

## Install and start

### Terminal 1 — API

```bash
git clone https://github.com/nasa1024/zi.git
cd zi
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn novelforge.app.main:app --host 127.0.0.1 --port 8787
```

Expected health response at <http://127.0.0.1:8787/health>:

```json
{"status":"ok","version":"0.3.0"}
```

### Terminal 2 — Studio

```bash
cd zi/web
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>.

## Five-minute no-key evaluation path

1. Select **Start writing** and create a project. A zero-input project is valid.
2. In Studio, load the included example setting or add a character, world rule, and constraint.
3. Open **World** and inspect the World Bible, facts, and as-of state projection.
4. Open **Plan** and inspect the story spine, future route choices, and chapter contract structure.
5. Open **Write** to inspect the contract, candidate-proof, alignment, and plan-version surfaces.
6. Open **Review** to see how model-proposed facts are approved or rejected before entering Canon.
7. Open **Settings / Memory** to inspect memory coverage and the rebuild controls.

The deterministic features above do not send data to an external service.

## Optional live-generation path

Copy `.env.example` to `.env`, configure one supported provider, and restart the API after exporting the file. For example:

```ini
NOVELFORGE_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key-here
```

Do not put provider credentials in `NOVELFORGE_API_KEY`; that variable protects NovelForge's own REST API.

## API-only smoke path

With the API running:

```bash
curl -s -X POST http://127.0.0.1:8787/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Use the returned project ID with the interactive OpenAPI interface at <http://127.0.0.1:8787/docs>. Sample world data is available in `config/bible_seed.yaml`.

## Verification commands

```bash
# Backend
.venv/bin/python -m pytest -q

# Frontend
cd web
npm run check
```

Expected result:

- 726 backend tests pass.
- TypeScript build, ESLint, Vitest, and Vite production build pass.

## Data and cleanup

Runtime projects, SQLite databases, drafts, and vector indexes are written under ignored local paths. They are not committed. Remove the locally created project data after evaluation if desired; no cloud account is created by the no-key path.

## Build Week evidence

- Core feature commit: `f8761e69e16f2525f5c62dd404b45200c3487b34`
- Codex model: `gpt-5.6-sol`
- Codex Session ID: `019f664f-c328-7453-8550-f664e6eead64`
- Detailed boundary: [BUILD_WEEK.md](../../BUILD_WEEK.md)
