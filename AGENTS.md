# AGENTS.md — Development Guidelines for Code Agents

Applies to all agents (Claude Code, Codex, …) working on this repository.
Quick overview and commands: see `CLAUDE.md`. Architecture & phases: `docs/PLAN.md`.

## Build & Run

| Purpose | Command |
|---|---|
| Desktop start (one-click) | `start.bat` (Windows) · `./start.sh` (Linux) |
| Server mode | `python run.py --server [--host H] [--port P]` |
| Backend only, custom port | `python run.py --no-browser --port 8711` |
| Tests | `python -m pytest tests/ -q` |
| Lint / format | `python -m ruff check .` · `python -m ruff format .` |

The venv lives under `.venv/`; `run.py` installs missing **core** dependencies on
its own. Feature groups (Whisper, embeddings, LLM, PDF) are installed at runtime by
the onboarding — in tests via `setup_check.install_group(...)` when needed.

## Architecture Invariants

1. **One process.** Backend, frontend serving and background workers run in the
   same Uvicorn process. No additional services, brokers or database servers.
2. **SQLite is the only database** (`data/app.db`), vectors via sqlite-vec in
   the same file. No external DB connections.
3. **Frontend without a toolchain.** ES modules directly in the browser; vendor
   libs checked in under `frontend/vendor/`. No npm/Vite/webpack.
4. **All long-running operations are jobs** with status reporting through the
   EventHub. No request handler blocks for longer than ~1 s.
5. **Paths are configurable** (models, workspaces, data) and are resolved
   exclusively through `config.py` — never hard-coded.
6. **Platform-neutral:** Windows and Linux are equals. `pathlib` instead of
   string paths, no shell calls without a Windows equivalent, ffmpeg via
   `setup_check.ffmpeg_path()`.

## Code Style

- Python ≥ 3.11, type annotations everywhere; `ruff` (check + format) must be clean.
- Pydantic models for all API inputs/outputs and settings.
- Log error paths (`logging.getLogger(__name__)`), never swallow silently.
- **Language of runtime texts:** log messages and console/script output are
  **English** (admin audience). Texts rendered in the web UI — `HTTPException`
  `detail` messages, job/setup progress messages, engine status details — are
  **German** (the UI default language).
- Frontend: ES modules, no class hierarchies needed; DOM construction via
  template strings + the `html()` helper in `frontend/js/dom.js`.
- UI: Material Design 3 via own CSS tokens, mobile first (bottom nav/FAB on
  smartphones, rail/multi-column on desktop) — principles in `docs/PLAN.md` §1.6.
- **No px units in styling.** Sizes in `rem`/`em`/relative values
  (`%`, `vw`, `fr`, …), media queries in `em` (base 1rem = 16px). Also applies
  to inline styles and `matchMedia` in JS. Otherwise `tests/test_pwa.py::
  test_no_px_units_in_frontend_styling` fails; only `frontend/vendor/` is exempt.
- Add every new frontend file to the `SHELL` list in `frontend/sw.js`
  (offline cache); `tests/test_pwa.py` enforces completeness.
- Code/comments/commits in English. UI texts via the i18n catalog
  (`frontend/i18n/`, at least de/en/ru); always add new strings to all
  catalogs, default language German.
- **Line endings come from `.gitattributes`**, not from a local
  `core.autocrlf`: the repository stores LF, only `*.bat` and `*.iss` are
  checked out as CRLF. Never rewrite a whole file's line endings as a side
  effect of an edit — a diff of thousands of lines hides the actual change.

## Test Routines

- After every code change, run `python -m ruff format --check backend/ tests/ run.py` and
  fix any reported files before considering the change complete.
- Every new route: test with FastAPI `TestClient` (`tests/`), including the error case.
- `conftest.py` sets `VERBA_DATA_DIR` to a tmp directory — tests must never
  touch the real `data/`.
- Always mock network access (downloads, LLM calls) in tests.
- Before finishing anything: `pytest` and `ruff check` green.

## Security & Behavior Rules

- No secrets in the repo or in logs (API keys only in `data/settings.json`,
  which is gitignored; mask them in API responses).
- Downloads only from the sources listed in `setup_check.py`, with a size limit.
- The server file browser (`/api/files/browse`, since phase 2) stays restricted
  to configured root directories — path traversal tests are mandatory.

## Commit Convention

Subject line with a type prefix (`New:`/`Fixed:`/`Changed:`/`Removed:`/`Refactor:`/`Docs:`/
`Tests:`/`Build:`), English. Multiple change types → one line each in the body.
