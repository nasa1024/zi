# Agent workflow

## Python package management

Use **uv** as the only Python package manager for this repository.

- Source dependencies are declared in `pyproject.toml`; the resolved environment is pinned in `uv.lock`.
- Do not run `pip install`, manually edit `.venv`, or create a second virtual environment.
- Add or change runtime dependencies with `uv add <package>`.
- Add or change test/development dependencies with `uv add --dev <package>`.
- After dependency changes, commit both `pyproject.toml` and `uv.lock`.

## Commands

```powershell
# Create/update the locked Python environment
uv sync --all-groups

# Run the backend test suite
uv run pytest -q

# Run the API locally
uv run uvicorn novelforge.app.main:app --host 127.0.0.1 --port 8787
```

## Frontend

The frontend is managed separately with npm from `web/`:

```powershell
cd web
npm ci
npm run check
```
