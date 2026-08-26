# Verba — User Guide

Verba turns audio recordings into editable text — fully local, with optional
AI cleanup/translation, PDF export, semantic search and a public
transcription API.

## Installation & starting {#install}

**Ready-made packages** (project releases page):

- **Windows:** `Verba-Setup-….exe` — double-click, wizard, start menu entry.
  To update, run the newer installer over the existing installation. Data is
  stored per user and survives updates.
- **Linux desktop:** `Verba-….AppImage` — make executable and run.
- **Linux server:** unpack `verba-server-….zip` and run
  `sudo ./deploy/install.sh` — sets up the systemd service and autostart;
  nginx/Caddy templates are included. On an existing installation, running
  the same command updates Verba automatically and keeps data and workspaces.

**From source:**

- **Windows:** double-click `start.bat`
- **Linux:** `./start.sh` in the project folder
- **Server:** `./start.sh --server --port 8710` — reachable via IP/domain,
  also behind a reverse proxy (forward the WebSocket `/ws`)

On first start the core components are set up automatically; the app opens in
your browser at `http://127.0.0.1:8710`.

**The address on start.** Every start prints what Verba listens on — in server
mode including the machine's own IP addresses, so it is clear where to point
another device:

```
------------------------------------------------------
Verba 0.1.0 - server mode
  listening on   http://0.0.0.0:8710  (all interfaces)
  local          http://127.0.0.1:8710
  network        http://192.168.1.50:8710
  data directory /opt/verba/data
  stop with Ctrl+C
------------------------------------------------------
```

As a **service** (systemd) the same block goes to the journal:
`systemctl status verba` shows it at the end, `journalctl -u verba` from the
beginning. The address is also the first line of the application log
(`data/logs/`), so it is available for a service that has been running for
weeks. If the port is taken, Verba says exactly that and does not start —
instead of a traceback.

In desktop mode, the **✕** button in the top right closes the Verba tab and
stops the local process. It also stops without that button: once the last
Verba tab (or the whole browser) is gone, it waits a few seconds for a reload
and then exits by itself. In server mode, Verba keeps running until its
service is stopped.

Verba is a **PWA**: the browser can "install" the app (icon in the address bar
or "Add to home screen"), after which it feels like a standalone app. The
interface also loads without a connection; as soon as the server is reachable
again, everything resumes automatically.

## First-run setup {#first-run}

The first-run setup walks through everything Verba needs, in five steps:

1. **Install components** — Verba checks the system (Python, ffmpeg, GPU, AI
   components) and installs whatever is missing. Progress is live: the bar
   covers the whole setup, and every component gets its tick as soon as it is
   installed and verified.
2. **Workspace** — where Verba keeps the transcript folders (section
   "Transcripts").
3. **Transcription** — default model, models directory, device and recording
   language (section "Whisper models").
4. **Language model** — optional: off, local or an OpenAI-compatible endpoint
   (section "Setting up a language model (LLM)").
5. **Search** — the embedding model for the semantic search (section
   "Search").

Every step can be left out with **Skip this step**; its default then applies
and can be changed in the settings at any time. **Set up later** leaves the
wizard entirely — the reminder that the setup is unfinished then stays. While
the first-run setup is in progress the navigation tabs are hidden; they appear
once you finish or leave it. The setup can be reopened later from
**Settings**.

If an installation fails, the components that are already done stay
installed; restarting Verba and trying again only cleans up the package that
was actually damaged.

## Transcripts {#transcripts}

Every transcript gets its own **workspace folder** on disk with `audio/`
(imported copies), `transcripts/` (JSON transcripts) and `exports/`. All of
them live inside the **workspace directory** from the settings (default:
`workspaces` next to the application, or in the data directory of an
installation). Absolute paths are what belongs there, network and removable
drives included (`M:\Transcripts`); `~` and `%USERPROFILE%` are expanded, and
a path pasted with quotes (as the Windows explorer copies it) is accepted. A
relative path is turned into an absolute one right away and shown that way in
the settings.

If you change the directory later, **all existing transcript folders move
along** — a background job moves them (seconds on the same drive; across
drives it takes as long as copying does) and updates the references in the
database. If the target directory already contains a folder of the same name,
the change is refused and nothing is moved; rename or move that foreign folder
yourself first.

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

## Transcript types {#types}

Six built-in types ship with the app: **Song** (song), **Interview/Dialog**,
**Speech** (speech), **Protocol**
(meeting minutes with summary and to-dos), **Poem** (poem) and
**Role play** (role play).

Each type sets a **layout** and carries **two prompts**, switched with a
dropdown in the editor.

The **layout** decides what the PDF export builds from and how it structures
without AI:

- **Paragraphs** — running text from the processed text (default).
- **Stanzas** — line breaks are preserved (song, poem).
- **Dialogue** — builds on the segments with their speakers instead of the
  merged text, every turn with its speaker (interview).
- **Script** — like dialogue, with character names additionally in capitals
  (roleplay, play).

This means a type you created yourself can use the speaker segments too —
which used to be tied to the built-in types.

The two prompts:

- **Cleanup prompt** — tells the AI how the transcript itself is processed
  (paragraphs, speakers, filler words, stanzas …).
- **Output-format prompt** — tells the AI how the processed text is split into
  blocks for the **PDF export**: paragraphs, headings, stanzas, dialogue
  turns, lists (e.g. decisions and to-dos) and separators. The built-in types
  ship a fitting one — stanza blocks for a song, lists for decisions and
  to-dos in minutes.

If the output-format prompt is left empty, the default applies; it then shows
as the field's placeholder, and "Insert default" brings it back for editing. A
**new type** starts out pre-filled with it, so it only needs adapting. If the
AI cannot follow the instruction, the export falls back to rule-based
structuring — a PDF is always produced.

Types are managed in their own **Types** tab (main navigation): the list for
selection next to the editor for name, layout and prompts — on the phone as a
list and a detail view, one at a time. The **+** button creates a new type;
built-in types can be edited and deleted too. "Restore default types" brings
deleted or modified defaults back (both prompts).

## Importing audio {#import}

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

## Transcribing {#transcribe}

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

## AI processing (cleanup & translation) {#ai}

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

## Editor & timeline {#editor}

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
- The editor's **PDF header** section stores a title, an addition and a
  place/date field per file. The header line then reads `Title (addition)` on
  the left and the place/date on the right: the addition goes in parentheses
  directly behind the title, separated by a single space. Title and recording
  date are suggested automatically. Empty fields leave no trace — no empty
  pair of parentheses, and no header at all when all three are empty.
- The right-hand field is **free text** and takes a place as well, e.g.
  `Munich, 28.01.1933`. A date written as `YYYY-MM-DD` is rendered as
  `DD.MM.YYYY` on export — even in the middle of the text; everything else is
  left as it is.
- File names may optionally follow `date_source-language_target-language_title_addition`;
  this pre-fills the language, optional translation target, and header fields.

## Whisper models {#whisper}

In the **Settings → Transcription** section, **one list** shows every model
with its status: installed models carry an "installed" badge and can be
deleted right there, missing ones are downloaded with one click. Rule of
thumb: `small` is a good start; `large-v3` gives the best quality but needs
much more power. Custom CTranslate2 models can be placed as folders in the
models directory (subfolders are detected) and appear in the same list.

**A model directory of your own** (e.g. an existing collection under
`M:\Modelle\whisper`) is entered under **Settings → Transcription** and takes
effect immediately, without a restart: Verba reads the directory from disk on
every request.

- Every subfolder holding a `model.bin` is found — nested ones too — and shows
  up as a model under its folder name.
- If a folder is named exactly like a built-in model (`large-v3`), that model
  counts as **installed** and is not downloaded again. The same goes for the
  HuggingFace cache (`models--Systran--faster-whisper-…`) as long as the
  download is complete.
- If the folder has a different name (`faster-whisper-large-v3`,
  `eigene/my-finetune`), it shows up as a model of its own and can be selected
  — it is just not marked as a built-in.
- Folders without a `model.bin` and aborted downloads are ignored.
- A path that does not exist yet is created. If it lives on a network drive
  that is currently not connected, the list stays empty.

## Setting up a language model (LLM) {#llm}

Under **Settings → AI processing (LLM)** a switch picks exactly **one** path —
only its fields are shown:

- **Off** — cleanup/translation disabled; everything else works normally
- **Local (llama.cpp)** — no external services at all: Verba shows the detected
  hardware (RAM, GPU/VRAM) with a model recommendation; one click installs
  llama.cpp, another downloads the chosen model. The local LLM server starts
  automatically on demand.
- **OpenAI-compatible endpoint** — base URL, API key and model name (works with
  OpenAI, Ollama, LM Studio, vLLM and others); "Test connection" probes the
  endpoint and lists available models.

**Which local models?** Verba ships a vetted list of multilingual instruct
models, ordered by the hardware they need:

| Model | Download | Needs at least |
| --- | --- | --- |
| Qwen3 1.7B (Q8) | approx. 2.1 GB | 4 GB RAM/VRAM |
| Qwen3 4B (Q4_K_M) | approx. 2.6 GB | 6 GB RAM/VRAM |
| Gemma 3 4B (Q4_K_M) | approx. 2.4 GB | 6 GB RAM/VRAM |
| Qwen3 8B (Q4_K_M) | approx. 5.2 GB | 10 GB VRAM / 20 GB RAM |
| Gemma 3 12B (Q4_K_M) | approx. 7.0 GB | 14 GB VRAM / 28 GB RAM |

The **star** marks the recommendation for your machine: Verba looks at the
VRAM (or half the RAM without a GPU) and suggests the largest fitting model of
the Qwen3 line. Nothing is decided automatically though — you download the
model yourself and can pick another one at any time; the **local model** field
shows which one the server uses.

**Your own models and your own directory.** **GGUF directory** sets the folder
the models live in (e.g. `F:\Models\llm`). Every `.gguf` file in it appears in
the selection and is **loaded straight from there** — nothing is copied and
nothing is downloaded twice. That is also how to use a model that is not in
the list at all: drop the file into the folder, pick it under **local
model**, done.

## PDF export {#pdf}

Transcribed files can be exported as PDF — via the PDF icon in the file row
or with **PDF export (all)** in step 3 of the action card for the whole
transcript. The dialog
lets you pick the text version: original (cleaned text, else the raw
transcript) or an existing translation.

Translations offer **two ways**, selectable in the dialog right below
"original":

- **One version per PDF** — you pick the original or one specific language.
  The file name carries the language as a suffix (`song.pdf`, `song.en.pdf`,
  `song.ru.pdf`), so every version sits next to the others in the `exports/`
  folder.
- **"Original + all translations (one PDF)"** — the original and every stored
  translation land in **one** document, one below the other, each separated by
  a centred `---` line. The file is called `song.all.pdf` and therefore never
  overwrites a single-version export. In a folder export this applies per
  file: header and original, then `---` and the translations, then the next
  file.

The appended versions do **not** repeat the header — it would be identical,
because title, addition and place/date are metadata of the file and are not
translated along. A header therefore marks a new file, and `---` marks a
language switch. Picking a single language that has no translation stored yet
makes the job fail with a message instead of quietly exporting the original; in
combined mode only the translations that exist are appended.

The export runs in two stages: with an LLM configured the AI structures the
text to match the transcript type (stanzas, speaker roles, minutes with
summary and to-dos); without one the structure is rule-based — the export
always works. The layout follows the type, e.g. invisible separators and
extra whitespace or a script layout for role plays.
Without a type a plain text PDF is produced.

**PDF export (all)** creates one collective PDF: each file follows as its own
section separated by spacing only — no table of contents and no extra titles.
Finished PDFs appear in the **Exports (PDF)** card for download or deletion;
in the workspace they live under `exports/`.

## Search {#search}

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
status, the embedding model and a button for a manual rebuild. The search
components are installed by the setup (feature group “Semantic search”).

**Embedding model.** The choice is a fixed list of vetted models — all
multilingual (German queries find English and Russian content) and
CPU-friendly:

| Model | Size | Languages | Character |
| --- | --- | --- | --- |
| MiniLM multilingual (default) | approx. 0.5 GB | 50 | fast |
| Multilingual E5 small | approx. 0.5 GB | 100 | balanced, a bit more precise |
| mpnet multilingual | approx. 1.0 GB | 50 | thorough, slower |
| BGE-M3 | approx. 2.3 GB | 100 | best quality, noticeably slower |

**BGE-M3** is the pick when quality matters more than speed — it is the only
model in the list whose download (approx. 2.3 GB) and CPU time you notice, and
its 1024 dimensions make the index larger too. On weaker machines or with very
many transcripts the default remains the better choice.

The selected model is downloaded automatically on first use — that needs an
internet connection once; after that the search works entirely offline. Where
to is set by **models directory (embeddings)** (default:
`<data>/models/embeddings`). If the model is already there it is **loaded from
there instead of downloaded again** — both a plain folder (`bge-m3/`,
`BAAI_bge-m3/`) and a moved HuggingFace cache
(`models--BAAI--bge-m3/snapshots/…`) are recognised. Such models are marked
"already on disk" in the pick list. Switching models invalidates every
stored vector and therefore starts a full reindex automatically. If the status
says the index came from another model, **Rebuild index** is all it takes.

## Settings {#settings}

The settings are organised into sections: on a phone — like in a native app —
a list of sections appears first; tapping one opens it as its own page ("All
settings" leads back). On desktop the section list sits as a sidebar next to
the selected section.

- **Interface:** language (German, English, Russian), documentation — the
  guide appears there as sections with an icon, each one collapsible. With a
  language model configured, **Ask about the guide** sits above it: type a
  question and the AI answers from this guide alone. The answer is formatted
  (paragraphs, lists, code) like the guide itself. Every question starts fresh
  (not a chat), and below the answer you see which sections it is based on. Verba only sends the sections that match the question; if even that does
  not fit the model's context, the selection is shrunk automatically instead
  of showing an error. Without a language model the input box is not there.
- **Transcription:** default model, models directory, device (GPU/CPU),
  compute precision, recording language — including the Whisper model
  management (download/delete) in the same section
- **AI processing (LLM):** off / local / endpoint, GGUF directory (section
  "Setting up a language model (LLM)")
- **Storage & logs:** workspace directory, server port, log level and
  retention (older logs are deleted automatically)
- **Search:** index status, embedding model (pick list), models directory,
  rebuild index
- **API:** keys for the public transcription API (section "Public API")
- **System:** information about the machine Verba runs on — CPU (model and
  cores), memory (free/total), graphics card with VRAM, ffmpeg status — plus
  the app version

## Public API {#api}

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
