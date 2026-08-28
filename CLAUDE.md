# Verba — Agent Guide

Cross-platform transcription tool: FastAPI backend + PWA frontend (no build step),
faster-whisper, optional LLM pipeline (OpenAI-compatible), PDF export, semantic
search (sqlite-vec).
Architecture & phase plan: `docs/PLAN.md`. Detailed guidelines: `AGENTS.md`.

## Commands

```bash
# Start development (creates venv, installs core deps, opens browser)
./start.sh              # Linux/macOS
start.bat               # Windows
python run.py --server --host 0.0.0.0 --port 8710   # server mode, no browser

# Tests & lint (from the project root, venv active)
python -m pytest tests/ -q
python -m ruff check backend/ tests/ run.py
python -m ruff format --check backend/ tests/ run.py
```

## Structure (short version)

- `run.py` — entry point; bootstraps core deps, desktop/server mode,
  frozen support (PyInstaller: per-user data, `<data>/site-packages` on sys.path);
  binds the listening socket itself and prints the address block (ASCII only —
  a Windows console codepage cannot encode more), which systemd captures in
  the journal
- `backend/verba/` — entire backend
  - `config.py` — settings (pydantic) ↔ `data/settings.json`; path resolution
    (configured directories are normalised to absolute paths; workspaces,
    whisper models, embeddings and GGUF each have their own configurable
    directory), curated `EMBEDDING_MODELS` catalog for the search
  - `db.py` — SQLite (`data/app.db`): projects, files, segments, jobs; short-lived connections
  - `logging_setup.py` — log rotation (retention from settings)
  - `setup_check.py` — first-run checks + automatic installation (ffmpeg, pip groups)
  - `events.py` — EventHub: WebSocket broadcast to the UI (`publish()` is threadsafe)
  - `lifecycle.py` — process lifetime: desktop mode stops when the last UI
    WebSocket stays away (grace period for reloads); server mode keeps running
  - `core/jobs.py` — persistent JobQueue: two lanes (main/llm), FIFO per session,
    priority for small jobs, LLM-location scheduling (remote → parallel,
    local → phased batches with model swap), cancellation, requeue after restart;
    every job row is read through `JOB_SELECT`, which joins the file name so the
    UI can say *which* file a step is running on
  - `services/` — workspace (project folders, import, moving the workspaces
    root incl. DB repointing via job kind `move_workspace`),
    hardware (the single RAM/VRAM probe — `setup_check`, whisper and llamacpp all
    read it; per-model memory verdicts `ok`/`tight`/`no` in German for local
    engines only, plus the OOM classifier that turns an allocation failure into
    a message and a CPU retry), whisper (model discovery,
    CPU fallback for broken CUDA and for a full VRAM, preflight refusal of a
    model that fits nowhere, range transcription), transcripts
    (segment CRUD + workspace JSON sync), audio (ffmpeg cutting), media (duration probe),
    llm (OpenAI-compatible client), llamacpp (local LLM: hardware probe,
    binary/GGUF download into the configured directory — files already there
    are loaded in place, `recommended` entries drive the hardware suggestion),
    pipeline (cleanup/translation,
    derived_texts, auto-chaining after transcription via the project's
    auto_process switch), chunking (segment boundaries + overlap), metadata (tags/file name),
    project_types (7 default types with cleanup prompt, output-format
    prompt and layout; seeding + per-field backfill via meta table), pdf (two-stage
    PDF export: LLM- or rule-based structuring → deterministic
    fpdf2 renderer; the type's layout (paragraphs/stanzas/dialogue/script)
    and output prompt drive both stages; compilation PDF without a table of
    contents; optional combined export (original + translations in one PDF,
    separated by a `---` divider);
    job kind `export_pdf` runs in the llm lane), vectorstore (global search index:
    small chunks with timestamps, FTS5 via DB triggers + sqlite-vec embeddings,
    hybrid query with RRF, auto-index after transcription/segment edit, reindex job
    on model change; the embedding model comes from the config catalog, its
    query/passage prefixes are applied per model, and a model already lying in
    the embeddings directory is loaded from disk instead of downloaded),
    rag (LLM answer with mandatory
    sources from search hits), docs_qa (single-shot questions about the user
    guide: section ranking, character budget, budget halving on a context-length
    error; only available with a configured LLM),
    public_api (public OpenAI-compatible API: key management with SHA-256 hashes,
    synchronous job bridge `api_transcribe` in the main lane, srt/vtt/verbose_json formatting)
  - `api/` — REST routers (system incl. `setup/complete` for the five-step wizard,
    settings incl. `settings/paths` and the workspace-move/reindex side effects of
    PUT, projects, types, files, segments, jobs, models, docs incl. `docs/ask`,
    export, search incl. `search/models`, apikeys, openai_compat →
    `/v1/audio/transcriptions`: Bearer auth as soon as a key exists, otherwise open)
  - `main.py` — app factory `create_app()`, mounts `frontend/` statically
- `frontend/` — PWA, vanilla ES modules, hash routing; **no npm, no bundler**;
  LLM output (help answers, RAG answers) goes through `js/markdown.js`:
  marked does not sanitize, so the parsed fragment is filtered against an
  allowlist — never `innerHTML` a model answer directly;
  job progress is presented through `js/jobs.js` (step label from the i18n
  catalog, status line, progress card for jobs without a file row);
  `sw.js` caches the complete shell — add new frontend files to the SHELL list
  (`tests/test_pwa.py` enforces this)
- `tests/` — pytest; `conftest.py` isolates `data/` via `VERBA_DATA_DIR`
- `packaging/` — PyInstaller spec, Inno Setup script, AppImage build, icons
- `deploy/` — systemd unit, nginx/Caddy examples, `install.sh` (headless server)
- `.github/workflows/` — CI (ruff+pytest, Ubuntu+Windows) and release pipeline
  (Windows installer, AppImage, server zip attached to every published release)

## Rules

- **No build step in the frontend.** Do not introduce npm dependencies; external
  JS libraries are vendored into `frontend/vendor/` and checked in.
- **Heavy Python packages** (faster-whisper, sentence-transformers, …) belong in
  feature groups in `setup_check.py`, never in `requirements/core.txt` — the core
  start must work within seconds on a fresh system.
- **Runtime data** only under `data/` (gitignored) or the configured
  workspace directory. Never write into the repo.
- Settings changes always go through the `config.py` models (validation), never raw JSON.
- Status-relevant operations (jobs, setup, AI calls) report progress through the
  EventHub (`events.py`) so the UI stays live. A job's `report()` message is
  shown verbatim in the web UI: German, and naming the file whenever the
  surrounding row does not (`tests/test_job_progress.py` guards the language).
- Code, identifiers and comments in English. UI texts come from the i18n
  catalog (`frontend/i18n/`, de/en/ru, default German) — no hard-coded UI
  strings in new logic.
- **Log and console output in English** (admin audience); backend texts shown
  in the web UI (`HTTPException` details, job/setup progress, engine status)
  in German — the UI default language.
- UI follows Material Design 3 (own CSS tokens, no framework) and is
  **mobile first**: the smartphone feels like an app, the desktop like a
  desktop application (details: `docs/PLAN.md` §1.6).
- **No px in styling.** Sizes exclusively in `rem`/`em`/relative units
  (`%`, `vw`, `fr`, …); media queries in `em`. Applies to CSS, inline styles
  and `matchMedia` calls in JS — `tests/test_pwa.py` enforces this.
  Base: 1rem = 16px.
- Tests for every new API route and every service; FastAPI's `TestClient` is enough.
- After every code change, run `python -m ruff format --check backend/ tests/ run.py` and
  fix any reported files before considering the change complete.
- **Maintain the in-app docs:** user-visible feature changes belong in the
  user guide `docs/user/{de,en,ru}.md` (in the app under Settings →
  Documentation) — always update all three languages. Every `## ` heading
  carries a language-independent `{#slug}` marker: the UI picks the section
  icon by it (`SECTION_ICONS` in `frontend/js/views/docs.js`) and hides the
  marker. Keep the sections in the same order in all three files
  (`tests/test_docs_qa.py` enforces it).

## Commit Messages

English, with a type prefix in the subject line: `New:` · `Fixed:` · `Changed:` · `Removed:` ·
`Refactor:` · `Docs:` · `Tests:` · `Build:`. Mixed commits name each type on its own
line in the body.
