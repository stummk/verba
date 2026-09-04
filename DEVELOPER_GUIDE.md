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

## Get Started

Diese Anleitung richtet sich an Personen, die Verba lokal aus dem Quellcode
entwickeln möchten. Verba benötigt keinen Frontend-Build und keinen separaten
Datenbankserver.

### 1. Voraussetzungen installieren

Benötigt werden:

- Git
- Python 3.11 oder neuer
- Visual Studio Code (empfohlen) mit einer Python-Erweiterung
- optional: FFmpeg für Audioverarbeitung; die In-App-Einrichtung kann FFmpeg
  beim ersten Start automatisch einrichten

Prüfe die Installation im Terminal:

```text
git --version
python --version
```

Unter Windows kann statt `python` auch `py -3` verwendet werden.

### 2. Repository klonen

```bash
git clone <REPOSITORY-URL>
cd verba
```

Öffne anschließend den Ordner `verba` in VS Code. Alle folgenden Befehle werden
im Projektordner ausgeführt.

### 3. Virtuelle Umgebung anlegen

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

Falls PowerShell das Aktivieren wegen der Ausführungsrichtlinie blockiert,
kann die Umgebung ohne Aktivierung verwendet werden:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements/core.txt -r requirements/dev.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Abhängigkeiten installieren

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements/core.txt -r requirements/dev.txt
```

Die großen optionalen Pakete für Whisper, Embeddings, LLM und PDF werden nicht
in die Kernabhängigkeiten installiert. Sie werden beim ersten Start in der
Anwendung unter **Einrichtung** als Feature-Gruppen nachinstalliert.

### 5. Anwendung starten

Für die normale Desktop-Entwicklung:

Windows:

```powershell
start.bat
```

Linux/macOS:

```bash
./start.sh
```

Die Startskripte erzeugen `.venv` bei Bedarf und starten `run.py`. Der Browser
wird automatisch geöffnet. Alternativ kann die Anwendung direkt gestartet
werden:

```bash
python run.py
```

Für Server- oder API-Entwicklung ohne automatisches Browserfenster:

```bash
python run.py --server --host 127.0.0.1 --port 8710
```

Beim Start schreibt Verba einen Adressblock in die Konsole (Modus, gebundene
Adresse, im Servermodus zusätzlich die IP-Adressen des Rechners, Datenordner).
Als systemd-Dienst landet derselbe Block im Journal, und die Adresse steht
außerdem in der ersten Zeile des Anwendungsprotokolls.

Danach ist die Anwendung unter `http://127.0.0.1:8710` erreichbar. Ist der
Port belegt, meldet Verba das in einer Zeile und startet nicht — verwende dann
zum Beispiel `--port 8711`.

### 6. Ersteinrichtung durchführen

Der Assistent führt in fünf Schritten durch die Einrichtung; jeder Schritt
lässt sich mit den Standardwerten überspringen, und **Später einrichten**
verlässt ihn ganz (der Hinweis auf die unfertige Einrichtung bleibt dann).

1. **Komponenten installieren** — fehlende Feature-Gruppen und ffmpeg.
2. **Arbeitsbereich** — Verzeichnis für die Transkript-Ordner.
3. **Transkription** — Whisper-Modell, Modellverzeichnis, Gerät, Sprache.
4. **Sprachmodell** — optional lokal (llama.cpp) oder OpenAI-kompatibel; nötig
   für Bereinigung, Übersetzung, Suchantworten, PDF-Struktur und die Fragen
   zur Hilfe.
5. **Suche** — Embedding-Modell aus dem Katalog.

Erst das Abschließen des letzten Schritts setzt `setup.completed`
(`POST /api/system/setup/complete`). Importiere danach eine kurze Audiodatei
und teste den vollständigen Ablauf.

Die Laufzeitdaten liegen während der Entwicklung unter `data/` und sind vom
Quellcode getrennt. Tests überschreiben diesen Pfad über `VERBA_DATA_DIR` und
verwenden niemals die echte Entwicklungsdatenbank.

Dieses Verzeichnis ist das *Basisverzeichnis* und gehört zur Installation:
`settings.json`, `site-packages` (liegt zur Laufzeit auf `sys.path`), das
heruntergeladene ffmpeg und die Modellverzeichnisse — alles nachladbar.

Das *Datenverzeichnis* enthält Datenbank und Protokolle und ist über
`general.data_dir` konfigurierbar. Weil beide im Betrieb geöffnet sind, wirkt
eine Änderung erst beim nächsten Start: `config.data_dir()` antwortet mit
`general.data_dir_active` (wo die Daten wirklich liegen), und
`datamove.apply_pending_move()` gleicht beide beim Start ab — vor der ersten
Datenbankverbindung und vor dem Logging.

### 7. Tests und Codequalität ausführen

Mit aktivierter `.venv`:

```bash
python -m pytest tests/ -q
python -m ruff check backend/ tests/ run.py
python -m ruff format backend/ tests/ run.py
```

Unter Windows ohne Aktivierung ersetze `python` durch
`.venv\Scripts\python.exe`. Führe die Tests vor jedem Commit aus. Neue API-
Routen werden mit FastAPI `TestClient` getestet; Netzwerkzugriffe und schwere
Modelle müssen in Tests gemockt werden.

### 8. Frontend entwickeln

Das Frontend ist bewusst ohne npm, Bundler und Build-Schritt aufgebaut:

1. Bearbeite die ES-Module unter `frontend/js/` und die Styles unter
  `frontend/styles.css`.
2. Lege sichtbare Texte in allen drei Katalogen unter `frontend/i18n/` an:
  `de.json`, `en.json` und `ru.json`.
3. Füge jede neue Frontend-Datei in die `SHELL`-Liste von `frontend/sw.js` ein.
4. Verwende keine `px`-Einheiten im Frontend; die PWA-Tests prüfen das.
5. Lade die Browserseite neu. Der laufende Server liefert die statischen Dateien
  direkt aus dem Projektordner.

### 9. Änderungen prüfen und committen

```bash
git status
git diff --check
python -m pytest tests/ -q
python -m ruff check backend/ tests/ run.py
git diff
git add <geänderte-dateien>
git commit -m "Changed: kurze Beschreibung"
```

Commit-Nachrichten beginnen mit einem englischen Präfix wie `New:`, `Fixed:`,
`Changed:`, `Tests:` oder `Docs:`.

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
  deletion, model change). The embedding model is a choice from
  `config.EMBEDDING_MODELS` (multilingual, CPU-sized); its query/passage
  prefixes are part of the catalog entry, and an unknown name in
  `settings.json` falls back to the default instead of breaking the search.
- **Workspace directory** (`services/workspace.py`): configured directories are
  stored absolute (`config.normalize_dir`). Changing the workspaces root moves
  every project folder there and rewrites the one absolute path per project —
  as job kind `move_workspace` in the `main` lane, so it never runs while a
  transcription reads from those folders. A name collision in the target is
  refused before anything is stored or moved.
- **Local LLM** (`services/llamacpp.py`): a curated GGUF catalog (Qwen3 and
  Gemma 3, ordered by hardware need) plus a hardware probe that *recommends*
  one of the `recommended` entries — it never picks for the user. The GGUF
  directory is configurable; every `.gguf` in it is selectable and loaded in
  place, so an existing collection needs no download and a model outside the
  catalog works too.
- **Documentation Q&A** (`services/docs_qa.py`): one question, one answer, no
  history. Sections of `docs/user/<lang>.md` are ranked against the question,
  the best few go into a character budget, and a context-length error from the
  endpoint halves the budget and retries — the user gets an answer, not a
  stack trace. Without a configured LLM the feature is absent, not disabled
  (`GET /api/docs` reports `llm_available`).
- **Public API** (`api/openai_compat.py`): OpenAI wire format,
  synchronous through the JobQueue; keys as SHA-256 hashes in `api_keys`.
- **Frontend**: no dependencies beyond the checked-in vendor files
  (`frontend/vendor/`). UI texts exclusively through the i18n catalog.
  Styling without fixed `px` units — only `rem`/`em`/relative values, media
  queries in `em` (base 1rem = 16px); `tests/test_pwa.py` enforces this.
- **Server binding**: desktop mode binds the full loopback
  (127.0.0.1 **and** ::1) so `localhost` works regardless of IPv4/IPv6
  resolution (`run.py:loopback_sockets`).
- **Process lifetime** (`lifecycle.py`): desktop mode follows its UI. When the
  last WebSocket client is gone, a watchdog waits `VERBA_IDLE_EXIT_SECONDS`
  (default 6 s, `0` disables) and then stops the process — long enough for a
  reload to reconnect, so closing the tab or the browser ends the app instead
  of leaving it in the background. It only arms after a UI has connected, so
  `--no-browser` starts are not killed. Server mode never self-exits.

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
- The first-run setup installs feature groups via bundled pip into
  `<data>/site-packages` (binary wheels only); `run.py` puts that directory
  on `sys.path` at startup. The installation runs in a **child process** —
  the executable re-invokes itself with `--internal-pip` — and the smoke
  test after it with `--internal-import`. The server process must never
  import a feature group itself: Windows locks a loaded `.pyd`, and pip then
  cannot replace shared dependencies (numpy under sentence-transformers)
  when a later group is installed. `setup_check.group_installed()` therefore
  only ever locates modules (`find_spec`).
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
4. **Repair marker:** a pip run that still hits a Windows file lock can
   leave a half-deleted package behind. `setup_check` writes the affected
   top-level names into a marker; at the next start
   `config.repair_site_packages()` removes exactly those, before anything
   imports from the directory, so the retry installs them cleanly. It must
   stay targeted — wiping the whole directory would also throw away the
   groups that already work.

### Windows Installer

```powershell
iscc /DAppVersion=1.0.0 packaging\verba.iss       # → dist\Verba-Setup-1.0.0.exe
```

For published Windows builds, the application and installer should be signed
with a publicly trusted Authenticode code-signing certificate. The Windows job
in `.github/workflows/release.yml` does this automatically when these two
GitHub repository secrets are configured:

- `WINDOWS_CODESIGN_CERTIFICATE_BASE64`: the PFX file encoded as Base64
- `WINDOWS_CODESIGN_CERTIFICATE_PASSWORD`: the PFX file password

The certificate must never be committed to the repository. The signature
prevents the "Unknown publisher" warning; SmartScreen may still show a
reputation warning for a new certificate, even when the signature is valid.

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

The version comes from the tag (`v1.0.0` → `1.0.0`) — it names the artifacts
and, via `packaging/stamp_version.py`, is written into
`backend/verba/__init__.py` before each of the three builds. That is why the
app reports the released version under Settings → System instead of whatever
was committed last; a manual bump before tagging is not needed, and the number
in the repository is only the value for runs from source
(`tests/test_version_stamp.py`).

Quality gate: `.github/workflows/ci.yml` (ruff + pytest, Ubuntu and Windows)
runs on every push.

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
