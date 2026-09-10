# Verba — Agent Guide

Cross-platform transcription tool: FastAPI backend + PWA frontend (no build step),
faster-whisper, optional LLM pipeline (OpenAI-compatible), PDF export, semantic
search (sqlite-vec), optional user management with per-transcript visibility.
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
  - `config.py` — settings (pydantic) ↔ `data/settings.json`; `auth.enabled` is
    backend-owned (only `/api/auth` writes it, never the settings form);
    `base_data_dir()` (env/frozen/repo) belongs to the installation and keeps
    settings.json, site-packages, tools and the models; `data_dir()` holds the
    database and the logs and is configurable (`general.data_dir`), answering
    with `general.data_dir_active` — where the data is right now; path resolution
    (configured directories are normalised to absolute paths; workspaces,
    whisper models, embeddings and GGUF each have their own configurable
    directory), curated `EMBEDDING_MODELS` catalog for the search
  - `db.py` — SQLite (`data/app.db`): projects, files, segments, jobs, users,
    sessions, project_shares; short-lived connections. `vacuum_if_needed()`
    compacts the file, but only once the freed pages are worth a full rewrite
    (`VACUUM_MIN_BYTES`/`VACUUM_MIN_SHARE`) — deleting rows never shrinks
    SQLite on its own. Every schema change is
    additive (`_migrate`/`add_missing`) so an installation upgrades in place —
    an index on a migrated column belongs in `_migrate`, not in `_SCHEMA`, which
    runs first (`tests/test_auth_migration.py`)
  - `datamove.py` — moving the data directory: `general.data_dir` is the wish,
    `general.data_dir_active` the reality, and `apply_pending_move()`
    reconciles them at the next start, before the first DB connection and
    before logging — nothing is open then. Moved are `app.db` (+ WAL
    sidecars), `logs/` and the default `workspaces/`, whose absolute paths in
    `projects.workspace` are rewritten
  - `logging_setup.py` — log rotation (retention from settings)
  - `procutil.py` — every subprocess spawn goes through here: a child console
    program would otherwise flash its own window on a Windows build that has
    no console (nvidia-smi, ffmpeg, pip, llama-server)
  - `setup_check.py` — first-run checks + automatic installation (ffmpeg, the
    CUDA libraries via `services/cudalibs.py`, pip groups); a single component
    can be installed on its own (`run_cuda_libs`) without ticking the wizard off
  - `events.py` — EventHub: WebSocket broadcast to the UI (`publish()` is threadsafe);
    an event about a transcript passes `project_id`/`file_id` and then only reaches
    clients that may see it — otherwise the status line names foreign files
  - `lifecycle.py` — process lifetime: desktop mode stops when the last UI
    WebSocket stays away (grace period for reloads); server mode keeps running
  - `core/jobs.py` — persistent JobQueue: two lanes (main/llm), FIFO per session,
    priority for small jobs, LLM-location scheduling (remote → parallel,
    local → phased batches with model swap), cancellation, requeue after restart;
    every job row is read through `JOB_SELECT`, which joins the file name so the
    UI can say *which* file a step is running on
  - `services/` — auth (optional user management: scrypt password hashing from the
    stdlib — no extra dependency —, cookie sessions stored as SHA-256, roles
    admin/user, the three visibilities private/shared/public and the SQL clause
    `visibility_clause()` that every cross-project query must join; deleting an
    account removes its private transcripts and hands the shared and public ones
    to the longest-serving admin, and the last admin can neither be deleted nor
    demoted; switching the feature off keeps accounts, owners and visibilities
    in the database, so `enable()` without credentials switches it straight
    back on), workspace (project folders, import, moving the workspaces
    root incl. DB repointing via job kind `move_workspace`),
    maintenance (the `vacuum` job: `request_vacuum()` after a project/file
    deletion and after a reindex, deduplicated via `job_queue.has_active`, and
    once more at startup where nothing else holds the database),
    hardware (the single RAM/VRAM probe — `setup_check`, whisper and llamacpp all
    read it; per-model memory verdicts `ok`/`tight`/`no` in German for local
    engines only, plus the OOM classifier that turns an allocation failure into
    a message and a CPU retry),
    cudalibs (the CUDA libraries CTranslate2 links against but no wheel
    brings — cuBLAS 12 and cuDNN 9: its own installable component next to
    ffmpeg, offered only where it helps (NVIDIA GPU, transcription not set to
    CPU) and never as an answer to a GPU whose driver does not answer, which
    `cuInit` asks about because a container without `/dev/nvidia-uvm` still
    lets `nvidia-smi` list the card. Installing them is half the job:
    `preload()` loads the pip wheels' libraries with RTLD_GLOBAL — no loader
    searches `site-packages/nvidia/*/lib` — so the fix works without a
    restart, which is what `generation()` tells whisper), whisper (model
    discovery,
    CPU fallback for broken CUDA and for a full VRAM, preflight refusal of a
    model that fits nowhere, range transcription), transcripts
    (segment CRUD + workspace JSON sync), audio (ffmpeg cutting), media (duration probe),
    llm (OpenAI-compatible client; `settings.llm.reasoning` turns a
    reasoning model down or off via `reasoning_effort` plus
    `chat_template_kwargs` — an endpoint that refuses those is remembered and
    served without them from then on), llamacpp (local LLM: hardware probe,
    binary/GGUF download into the configured directory — files already there
    are loaded in place, `recommended` entries drive the hardware suggestion.
    The binary is not one download but a ladder (`_candidates`), walked until a
    build is *proven* to reach the card: the platform's GPU package (Windows
    CUDA, Linux Vulkan — no CUDA package exists for Linux), then a CUDA build
    compiled here (`llamabuild`), then the CPU build. `_probe_devices` asks the
    installed binary via `--list-devices` — `[]` means "no GPU" and rejects the
    rung, `None` means "could not be asked" and is kept on trust; a rejected
    attempt takes its directory with it, so `server_binary()` stays
    unambiguous, and `backend.json` records what was accepted for the status.
    A rung that raises is a failed attempt, never the end of the ladder, or a
    full disk would cost the 17 MB CPU build over the 600 MB CUDA one.
    The ladder never runs over an existing installation: `install_binary(force=True)`
    is the deliberate replacement of a build that computes on the processor,
    and it is not destructive — the old installation stays until
    `_accept_install` has a proven replacement, so a release that cannot be
    reached leaves the machine with the llama.cpp it had. And
    `ensure_backend_recorded()` measures an installation that predates the
    marker once — a certain answer is written down, an uncertain one only
    remembered for this process, so one timed-out probe cannot brand a
    working CUDA build "unknown" for good),
    llamabuild (compiling llama.cpp with CUDA on the machine: its own tool
    table for the package installer, `CMAKE_CUDA_ARCHITECTURES=native`, and a
    compiler count bounded by RAM rather than cores — `-j$(nproc)` with nvcc
    is how a container OOMs unreadably. Linux only, and never the answer to a
    driver that does not answer),
    pipeline (cleanup/translation,
    derived_texts, auto-chaining after transcription via the project's
    auto_process switch; `run_cleanup` is where the fork lives, because that
    is where the type is known: a type that reproduces its material goes
    chunk-local through `cleanup_segments` (which never condenses and never
    reads the whole recording itself) with the overview's title and spellings
    as a glossary — never the summary —, a type with `condense` on has its
    prompt run once over the whole recording via `overview.reduce_document`,
    so one title and one list of decisions instead of one per chunk. Which of
    the two runs is `condense`, never `verbatim`: what the export may do with
    a text and how that text came about are two questions, so a type may have
    its recording rewritten and still be exported word for word. For a
    reproducing type the promise does not rest on the prompt:
    `_keeps_the_text()` measures how much of a section survived its answer,
    asks again without the glossary where there was one, and then fails the
    step rather than storing a summary),
    overview (the whole recording in one bounded text: a digest per chunk
    (map), neighbouring digests condensed until they fit one call (fold),
    then a single call for what the document has only once (reduce) — the
    title/summary/terms the chunk-local steps orient themselves by, or the
    finished document of a condensing type. No call is ever given more than
    a chunk's worth, cached per file in `file_overviews` and dropped when the
    segments change; `ensure()` keeps the digests but describes them again
    when that one call had failed, so a bad answer costs no title for good.
    The title is offered to the file (`workspace.apply_suggested_title`),
    which takes it only where neither the name scheme nor a tag stated one),
    chunking (segment boundaries + overlap), metadata (tags/file name),
    project_types (7 default types with cleanup prompt, output-format
    prompt, layout and two independent switches — `verbatim` (`is_verbatim()`)
    for what the export may do, `condense` (`condenses()`) for how the
    aufbereitung runs; seeding + per-field backfill via meta table, and
    `condense` is carried over from `verbatim` once in `db._migrate` so an
    installation keeps the way its types worked),
    pdf (two-stage
    PDF export: deterministic or LLM structuring → deterministic
    fpdf2 renderer; the type's layout (paragraphs/stanzas/dialogue/script)
    and output prompt drive both stages; a `verbatim` type (the default, every
    builtin but `protocol`) never reaches the LLM — `_structure_rule_based()`
    is lossless by construction, so the cleanup and the translations arrive
    word for word with no invented heading, which is the whole promise of the
    export (`tests/test_export_verbatim.py`); compilation PDF without a table of
    contents; optional combined export (original + translations in one PDF,
    separated by a `---` divider); a type may keep its sections whole
    (`keep_sections`): a file's documents form one group (`section_groups()`),
    are measured with fpdf2's `offset_rendering()` and start on a new page
    when they no longer fit — never in front of an already empty page;
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
    synchronous job bridge `api_transcribe` in the main lane, srt/vtt/verbose_json formatting),
    download (the one resumable downloader: HTTP range retries, free-space
    check, percent callback — used by llamacpp and updates),
    updates (Verba updates itself from its own GitHub releases: the newest tag
    against `__version__`, then the artifact this installation came from —
    Windows installer silently, AppImage by replacing the running file, server
    package by replacing the application files plus a pip run. The version
    being replaced is removed; only PACKAGE_ITEMS is touched, so database,
    logs, settings and workspaces stay. The relaunch is done by `run.py` after
    uvicorn stopped (port free): desktop installations are started again, a
    systemd service exits with code 42. The installation log lives in the
    process only — an update ends in a restart — and `cleanup_downloads()`
    wipes the artifact at the next start. A source checkout is left to git,
    and the whole check can be switched off),
    osupdate (the machine itself, not Verba: a Linux server installation
    updates its system packages with `apt-get update` and
    `apt-get --yes upgrade`, non-interactively and keeping the installed
    configuration; a checkbox makes it `dist-upgrade` plus `autoremove`
    instead, which is off by default because both may remove packages and is
    decided per run. Every line apt says is broadcast as `system.upgrade` and
    kept in the process only, so the settings page can show the run while it
    happens. Offered nowhere else — not on Windows, not for the AppImage, not
    in desktop mode — and only with root or passwordless sudo; a reboot a
    package asks for is reported, never performed)
  - `api/` — REST routers; `deps.py` holds the whole authorisation surface
    (`current_user`, `require_admin`, `project_or_403`, `file_or_403`) and returns
    a permissive result while the user management is off, so there is no second
    unguarded code path. `main.py` adds a middleware that 401s every `/api` route
    without a session, so forgetting a per-route check cannot open a hole.
    Routers: auth (login/logout/state/enable/disable/own password/own account),
    users (admin CRUD + `users/directory` for the share picker),
    system incl. `setup/complete` for the six-step wizard,
    settings incl. `settings/paths` and the data-dir/workspace-move/reindex
    side effects of PUT, projects, types, files, segments, jobs, models, docs incl. `docs/ask`,
    export, search incl. `search/models`, apikeys, openai_compat →
    `/v1/audio/transcriptions`: Bearer auth as soon as a key exists or the user
    management is on, otherwise open)
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
- The session cookie is marked `Secure` from `request.url.scheme`, which uvicorn
  rewrites from `X-Forwarded-Proto` — TLS terminated at a reverse proxy is
  therefore recognised. That header is trusted from `127.0.0.1` only
  (`FORWARDED_ALLOW_IPS` widens it); `auth.cookie_secure` overrides the whole
  decision.
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
- **Every new route that touches a transcript** goes through `api/deps.py`
  (`project_or_403`/`file_or_403`), and every query that spans projects joins
  `auth.visibility_clause()` — the search index is global, so that is where a
  private transcript would otherwise leak.
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
