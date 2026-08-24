# Verba — User Guide

Verba turns audio recordings into editable text — fully local, with optional
AI cleanup/translation, PDF export, semantic search and a public
transcription API.

## Installation & starting

**Ready-made packages** (project releases page):

- **Windows:** `Verba-Setup-….exe` — double-click, wizard, start menu entry.
  Data is stored per user and survives updates.
- **Linux desktop:** `Verba-….AppImage` — make executable and run.
- **Linux server:** unpack `verba-server-….zip` and run
  `sudo ./deploy/install.sh` — sets up the systemd service and autostart;
  nginx/Caddy templates are included.

**From source:**

- **Windows:** double-click `start.bat`
- **Linux:** `./start.sh` in the project folder
- **Server:** `./start.sh --server --port 8710` — reachable via IP/domain,
  also behind a reverse proxy (forward the WebSocket `/ws`)

On first start the core components are set up automatically; the app opens in
your browser at `http://127.0.0.1:8710`.

Verba is a **PWA**: the browser can "install" the app (icon in the address bar
or "Add to home screen"), after which it feels like a standalone app. The
interface also loads without a connection; as soon as the server is reachable
again, everything resumes automatically.

## First-run setup

On first launch Verba checks the system (Python, ffmpeg, GPU, AI
components) and installs whatever is missing — with live progress. The setup
can be reopened later from **Settings**.

## Transcripts

Every transcript gets its own **workspace folder** on disk with `audio/`
(imported copies), `transcripts/` (JSON transcripts) and `exports/`.

- New transcript: **+** button (bottom right). If you leave the name empty,
  today's date (`yyyymmdd`) is used.
- When creating a transcript you can pick a **transcript type** (section
  "Transcript types") — it
  steers how the AI processing treats the text. Without a type the transcript
  yields plain, unformatted text.
- Deleting a transcript can optionally remove the workspace folder too.

On import Verba extracts **metadata** automatically: title and date from MP3
tags and from filenames following the `YYYYMMDD_Title` scheme (e.g.
`20240817_Title.mp3`).

## Transcript types

Six built-in types ship with the app: **Song** (song), **Interview/Dialog**,
**Speech** (speech), **Protocol**
(meeting minutes with summary and to-dos), **Poem** (poem) and
**Role play** (role play). Each type carries a system prompt telling the AI
how to process transcripts of that kind.

Types are managed in their own **Types** tab (main navigation): the list for
selection next to the editor for name and system prompt — on the phone as a
list and a detail view, one at a time. The **+** button creates a new type;
built-in types can be edited and deleted too. "Restore default types" brings
deleted or modified defaults back.

## Importing audio

The action card in a transcript organises the workflow into three tabs:
**1. Import audio → 2. Transcribe & process → 3. Export.** Tapping a tab
shows exactly that step's actions; once files exist, step 2 is preselected.

Three equivalent ways:

1. **Upload** — file picker via the "Upload files" button
2. **Import from server** — browse folders on the machine/server;
   importing a folder picks up all audio files inside (nested too)
3. **Drag & drop** — drop files or whole folders onto the transcript view

Supported formats: mp3, wav, m4a, flac, ogg, opus, aac, wma, webm, mp4.
Importing always copies — your original files stay untouched.

## Transcribing

- **Single file:** microphone icon in the file row (finished files show a
  repeat icon for another run instead)
- **Everything:** "Transcribe all" in step 2 of the action card
- **Advanced (expandable):** change the Whisper model and recording language
  for this run only — saved settings stay unchanged
- Progress appears live per file; running jobs can be cancelled
- Tip: setting the recording language explicitly (instead of auto-detect)
  noticeably improves results

**Queue:** all jobs run through one central queue so the hardware is never
oversubscribed — even with several people working at once. Waiting files show
their queue position; small jobs (re-transcribing a section, audio edits) jump
ahead, and the order stays fair per user.

## AI processing (cleanup & translation)

Once a language model is configured (section "Setting up a language model
(LLM)"), transcribed files get a
**sparkle icon (AI processing)** and step 2 of the action card gains
**AI processing (all)**:

- **Cleanup** removes filler words and false starts, fixes punctuation and
  obvious mishearings — the transcript type flows in as context (a "revelation"
  is only minimally corrected, "minutes" get a summary and a to-do list)
- **Translate** renders the cleaned text (or the raw transcript) into nearly
  any language — all ~100 languages Whisper knows are available as targets
- Long recordings are split automatically along segment boundaries (with
  overlap) so even local models with small context windows work cleanly
- Results appear as tabs in the AI dialog and in the editor, and are also
  written as Markdown files into the workspace under `transcripts/`

**Full automation:** in the transcript view you can enable **"Process
automatically"** (optionally with a target language). Every finished
transcription then triggers the cleanup — and, if set, the translation — on its
own, without a single further click. Triggering individual steps manually
remains possible at any time.

**Scheduling:** if the LLM runs on another machine (external API),
transcription and AI processing run in parallel. If it runs locally on the
same system, Verba works in phases: first all transcriptions, then — after a
single model swap — all AI processing. Whisper and the LLM share the memory
without thrashing each other.

## Editor & timeline

Actions appear as icons with tooltips (hovering shows the description). The
document icon ("Open in editor") on a transcribed file opens the editor — a
**workspace** for audio,
transcript and AI texts:

- **Waveform** with play/pause; clicking a segment timestamp jumps the audio
  there; during playback the active segment is highlighted
- **Three areas** below the timeline: *segments* (original transcript),
  *cleaned* and *translation* — all directly editable with automatic saving.
  On large displays the areas sit **side by side** like in a desktop app and
  can be toggled individually; on the phone the same tabs switch between the
  views — with the full feature set preserved.
- If an AI text does not exist yet, it can be **created right from the editor**
  ("Create cleanup", "Create translation" with a language picker); the result
  appears live in the area once it is ready
- **Text and speaker** are edited directly in the segment rows — changes are
  saved automatically ("Saved" indicator), **Undo** reverts recent changes
  step by step
- **Selection** by dragging on the waveform, then:
  - **Re-transcribe selection** — only this section is recognised again and
    replaces exactly the affected segments
  - **Trim to selection / Remove selection** — audio cutting via ffmpeg;
    the result becomes a *new* file in the transcript, the original is kept

## Whisper models

In the **Settings → Transcription** section, **one list** shows every model
with its status: installed models carry an "installed" badge and can be
deleted right there, missing ones are downloaded with one click. Rule of
thumb: `small` is a good start; `large-v3` gives the best quality but needs
much more power. Custom CTranslate2 models can be placed as folders in the
models directory (subfolders are detected) and appear in the same list.

## Setting up a language model (LLM)

Under **Settings → AI processing (LLM)** a switch picks exactly **one** path —
only its fields are shown:

- **Off** — cleanup/translation disabled; everything else works normally
- **Local (llama.cpp)** — no external services at all: Verba shows the detected
  hardware (RAM, GPU/VRAM) with a model recommendation; one click installs
  llama.cpp, another downloads the chosen Qwen3 model. The local LLM server
  starts automatically on demand.
- **OpenAI-compatible endpoint** — base URL, API key and model name (works with
  OpenAI, Ollama, LM Studio, vLLM and others); "Test connection" probes the
  endpoint and lists available models.

## PDF export

Transcribed files can be exported as PDF — via the PDF icon in the file row
or with **PDF export (all)** in step 3 of the action card for the whole
transcript. The dialog
lets you pick the text version: original (cleaned text, else the raw
transcript) or an existing translation.

The export runs in two stages: with an LLM configured the AI structures the
text to match the transcript type (stanzas, speaker roles, minutes with
summary and to-dos); without one the structure is rule-based — the export
always works. The layout follows the type, e.g. invisible separators and
extra whitespace or a script layout for role plays.
Without a type a plain text PDF is produced.

**PDF export (all)** creates one collective PDF: each file follows as its own
section separated by spacing only — no table of contents and no extra
titles; the per-file header (title and date) comes from the template.
Finished PDFs appear in the **Exports (PDF)** card for download or deletion;
in the workspace they live under `exports/`.

## Search

The **Search** tab searches all transcripts at once — semantically (meaning
counts; German questions also find English or Russian content) and via full
text (proper names and rare terms match exactly). Under **Filters** the
search can be narrowed to one transcript, a type, language, speaker and a
date range.

Every hit shows transcript, file, timestamp and passage — a click opens the
editor exactly there, and the audio starts at that position.

With an LLM configured, **“AI answer with sources”** produces an answer that
backs every statement with numbered sources; sources are clickable like hits.
The AI answers only from the retrieved passages — if there are none, it says
so honestly instead of guessing.

New transcriptions and segment edits are indexed automatically; deleted files
disappear from the index immediately. **Settings → Search** shows the index
status, the embedding model (changing it automatically rebuilds the whole
index) and a button for a manual rebuild. The search components are installed
by the setup (feature group “Semantic search”).

## Settings

The settings are organised into sections: on a phone — like in a native app —
a list of sections appears first; tapping one opens it as its own page ("All
settings" leads back). On desktop the section list sits as a sidebar next to
the selected section.

- **Interface:** language (German, English, Russian), documentation
- **Transcription:** default model, models directory, device (GPU/CPU),
  compute precision, recording language — including the Whisper model
  management (download/delete) in the same section
- **AI processing (LLM):** off / local / endpoint (section "Setting up a
  language model (LLM)")
- **Storage & logs:** workspace directory, server port, log level and
  retention (older logs are deleted automatically)
- **Search:** index status, embedding model, rebuild index
- **API:** keys for the public transcription API (section "Public API")
- **System:** information about the machine Verba runs on — CPU (model and
  cores), memory (free/total), graphics card with VRAM, ffmpeg status — plus
  the app version

## Public API

Verba provides an OpenAI-compatible transcription API so external programs can
transcribe audio files — scripts, other servers, or anything that speaks the
OpenAI SDK.

- **Endpoint:** `POST /v1/audio/transcriptions` (multipart, OpenAI wire format)
- **Response formats** via `response_format`: `json` (default), `text`, `srt`,
  `vtt`, and `verbose_json` (with segments and timestamps)
- **Language:** optional via `language` (ISO code like `de`); otherwise
  detected automatically
- **Model:** `model=whisper-1` (or omitted) uses the Whisper model chosen in
  the settings; a concrete name like `model=small` picks a different model for
  this one request
- **Post-processing:** `model=whisper-1+cleanup` returns text cleaned up by the
  AI model (requires a configured LLM). `project_type=<key>` (e.g.
  `interview`) additionally cleans with that transcript type's prompt. In
  `srt`/`vtt`/`verbose_json` the segments remain the raw Whisper segments —
  only the text is cleaned.
- Every request runs through the same fair queue as the app and responds as
  soon as the transcription is done.

**API keys:** Keys can be created and deleted in the settings under "API". As
soon as at least one key exists, the endpoint requires
`Authorization: Bearer <key>`; without any key the API is open — intended for
strictly local use only. Each key is shown in plaintext exactly once; after
that only its beginning is visible.

Example with curl:

```bash
curl -X POST http://localhost:8710/v1/audio/transcriptions \
  -H "Authorization: Bearer vb-YOURKEY" \
  -F "file=@recording.mp3" \
  -F "language=en" \
  -F "response_format=srt"
```

Example with the OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8710/v1", api_key="vb-YOURKEY")
with open("recording.mp3", "rb") as audio:
    result = client.audio.transcriptions.create(model="whisper-1", file=audio)
print(result.text)
```
