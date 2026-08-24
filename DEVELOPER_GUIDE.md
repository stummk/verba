# Verba — Developer Guide

Technical guide for development, testing, packaging and deployment.
The user's perspective is covered by the user guide (`docs/user/`, in the app
under Settings → Documentation).

## Overview

Verba is a single-process system: one FastAPI backend serves the REST API,
WebSocket and the static PWA frontend. No database server, no message broker,
no frontend build — deliberate decisions in favor of one-click installation on
desktop and server.

```
run.py                     entry point (desktop/server mode, frozen support)
backend/verba/
  main.py                  app factory create_app(), router registration, lifespan
  config.py                settings (pydantic) ↔ data/settings.json, path resolution
  db.py                    SQLite (data/app.db), schema + additive migrations
  events.py                EventHub: WebSocket broadcast (publish() is threadsafe)
  setup_check.py           first-run checks, ffmpeg/pip installation, system info
  core/jobs.py             persistent JobQueue (two lanes, fairness, cancellation)
  services/                domain logic (whisper, pipeline, pdf, vectorstore, rag, …)
  api/                     REST routers, one module per resource
frontend/                  PWA: vanilla ES modules, hash routing, Material 3
tests/                     pytest, isolated data/ via VERBA_DATA_DIR
packaging/                 PyInstaller spec, Inno Setup, AppImage build
deploy/                    systemd unit, nginx/Caddy examples, install.sh
.github/workflows/         CI (ruff + pytest) and release pipeline
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
python -m pip install -r requirements/core.txt -r requirements/dev.txt
python run.py                                     # desktop mode, opens the browser
python run.py --server --port 8710                # server mode
```

Heavy packages (faster-whisper, sentence-transformers, fpdf2) are installed by
the **in-app first-run setup** — they never go into `requirements/core.txt`;
they belong in a feature group in `setup_check.py`.

### Tests & Lint

```bash
python -m pytest tests/ -q
python -m ruff check backend/ tests/ run.py
python -m ruff format backend/ tests/ run.py
```

Every new API route and every service gets tests (FastAPI's `TestClient` is
enough; `conftest.py` isolates the data directory per test).
Two consistency tests deserve special attention:

- `test_i18n_catalogs.py` — the three catalogs `frontend/i18n/{de,en,ru}.json`
  must carry the same keys; `test_pwa.py` additionally checks that every key
  used in the JS exists.
- `test_pwa.py` — every new frontend file must go into the `SHELL` list of
  `frontend/sw.js`, otherwise it is missing offline. It also enforces the
  styling rule: no px units anywhere in the frontend.

### Architecture Cornerstones

- **JobQueue** (`core/jobs.py`): two lanes. `main` = transcription/audio
  (strictly serial, hardware protection), `llm` = LLM post-processing + PDF
  export. A remote LLM runs in parallel with transcription; a local LLM is
  batched in phases (unload Whisper → LLM batch → back). Fairness: priority,
  then round-robin across `session_id` (FIFO per session). Jobs survive
  restarts (requeue at startup).
- **EventHub** (`events.py`): everything status-relevant (jobs, setup, engines)
  is published as a WebSocket event; the UI is purely event-driven.
- **Search** (`services/vectorstore.py`): small chunks with timestamps,
  FTS5 (kept in sync by DB triggers) + sqlite-vec embeddings, fusion via RRF.
  Index consistency is automatic (after transcription, segment edit,
  deletion, model change).
- **Public API** (`api/openai_compat.py`): OpenAI wire format,
  synchronous through the JobQueue; keys as SHA-256 hashes in `api_keys`.
- **Frontend**: no dependencies beyond the checked-in vendor files
  (`frontend/vendor/`). UI texts exclusively through the i18n catalog.
  Styling without fixed `px` units — only `rem`/`em`/relative values, media
  queries in `em` (base 1rem = 16px); `tests/test_pwa.py` enforces this.
- **Server binding**: desktop mode binds the full loopback
  (127.0.0.1 **and** ::1) so `localhost` works regardless of IPv4/IPv6
  resolution (`run.py:loopback_sockets`).

## Packaging & Release

### PyInstaller (desktop builds)

```bash
python -m pip install pyinstaller
pyinstaller packaging/verba.spec --noconfirm      # → dist/verba/ (one-dir)
```

The build bundles core dependencies, frontend and user guide —
**not** the heavy feature groups (~49 MB one-dir). In frozen mode:

- Runtime data lives per user (`%LOCALAPPDATA%\Verba` or
  `~/.local/share/verba`), never in the installation directory.
- The first-run setup installs feature groups via bundled pip
  (in-process, binary wheels only) into `<data>/site-packages`; `run.py`
  puts that directory on `sys.path` at startup.
- Bundled resources (frontend/, docs/user/) are resolved through
  `config.bundle_root()`.

Four frozen-mode specifics the spec handles deliberately (do not remove):

1. **pip as a file tree** (`pip-lib/`), not as frozen modules — pip's
   vendored `distlib` can only locate resources through path-based importers.
2. **Bundle `python3.dll`** (Windows): stable-ABI wheels such as PyAV link
   against `python3.dll`; PyInstaller by itself only ships `python3XY.dll`.
3. **The complete stdlib including submodules** as hidden imports: packages
   installed at runtime import arbitrary stdlib parts (fpdf2 →
   `timeit`, `unittest.mock`) that static analysis cannot see.
4. **Repair marker:** if the in-process installation fails, loaded `.pyd`
   files can leave half-deleted packages behind (Windows file locks).
   `setup_check` then sets a marker; at the next start
   `config.ensure_runtime_site_packages()` wipes the directory and the
   setup reinstalls cleanly.

### Windows Installer

```powershell
iscc /DAppVersion=1.0.0 packaging\verba.iss       # → dist\Verba-Setup-1.0.0.exe
```

### Linux AppImage

```bash
bash packaging/build_appimage.sh 1.0.0            # → dist/Verba-1.0.0-x86_64.AppImage
```

### Release Pipeline

`.github/workflows/release.yml` automatically builds three artifacts for every
published GitHub release and attaches them to it:

1. `Verba-Setup-<version>.exe` — Windows one-click installer
2. `Verba-<version>-x86_64.AppImage` — Linux desktop
3. `verba-server-<version>.zip` — headless server package

The version comes from the tag (`v1.0.0` → `1.0.0`). Quality gate:
`.github/workflows/ci.yml` (ruff + pytest, Ubuntu and Windows) runs on every
push.

## Server Deployment

The server zip (or a checkout) contains everything needed under `deploy/`:

```bash
sudo ./deploy/install.sh                # → /opt/verba, venv, systemd, autostart
```

- `deploy/verba.service` — systemd unit (user `verba`, restart, hardening)
- `deploy/nginx-verba.conf` — reverse proxy incl. WebSocket upgrade for `/ws`,
  2 GB upload limit, long timeouts for synchronous `/v1` transcriptions
- `deploy/Caddyfile` — the same for Caddy (TLS automatic)

All URLs in the frontend are relative; no path configuration is needed behind
the proxy. Health check: `GET /health`. Logs: `data/logs/`
(rotation, retention via settings) plus `journalctl -u verba`.

Anyone running the public API on a network should create at least one key in
the settings under "API" — only then does `/v1` require Bearer auth.
