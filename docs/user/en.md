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

The first-run setup walks through everything Verba needs, in six steps:

1. **Install components** — Verba checks the system (Python, ffmpeg, GPU, AI
   components) and installs whatever is missing. Progress is live: the bar
   covers the whole setup, and every component gets its tick as soon as it is
   installed and verified — the optional semantic search included, which
   therefore stays installable at any time. While the installation runs,
   **Next**, **Skip this
   step** and **Set up later** are locked — they become usable again as soon as
   the installation has finished or has failed.
2. **Storage** — the data directory (database and logs) and the workspace
   Verba keeps the transcript folders in (section "Transcripts"). A data
   directory changed here is used from the next start on — see "Settings".
3. **Transcription** — default model, models directory, device and recording
   language (section "Whisper models").
4. **Language model** — optional: off, local or an OpenAI-compatible endpoint
   (section "Setting up a language model (LLM)"). Choosing **Local
   (llama.cpp)** lets you install llama.cpp right there; a log shows every
   step as it happens.
5. **Search** — the embedding model for the semantic search (section
   "Search").
6. **Access** — optionally create an administrator account and switch the user
   management on (section "Users & visibility"). If the step is skipped Verba
   stays unprotected — which only makes sense for local use on this machine.

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
3. **Drag & drop** — drop files or whole folders onto the dashed area in the
   "Import audio" step; that area is the only drop target in the app

Supported formats: mp3, wav, m4a, flac, ogg, opus, aac, wma, webm, mp4.
Importing always copies — your original files stay untouched.

While files are uploading (drag & drop included) a progress card shows which
file is going over the wire (“File 2 of 7”), how far the whole selection has
come and when the server is storing the file.

## Transcribing {#transcribe}

- **Single file:** microphone icon in the file row (finished files show a
  repeat icon for another run instead)
- **Everything:** "Transcribe all" in step 2 of the action card
- **Advanced (expandable):** change the Whisper model and recording language
  for this run only — saved settings stay unchanged
- Progress appears live per file; running jobs can be cancelled
- Tip: setting the recording language explicitly (instead of auto-detect)
  noticeably improves results

**Where the progress is shown.** Every step reports which file it is working
on and how far it has got:

- **In the file row** (transcript view): a bar and a text, e.g.
  `lied.mp3: 01:23` while transcribing (percent = position in the recording),
  `Bereinigung 2/5` and `Übersetzung 1/3` during AI processing,
  `Warteschlange: Position 3` while waiting. "Transcribe all" and "AI
  processing (all)" create one job **per file**, so every row has its own bar.
- **In the top bar**: a summary that keeps running while you switch views —
  `KI-Aufbereitung — lied.mp3: Bereinigung 2/5 · 40 %`. With two jobs in
  parallel (transcription and processing) both are named, further ones as
  `+2 more`.
- **In the transcript list**: a `3 running` marker on a transcript while
  something is running there.
- **As its own card**: jobs that belong to no single file — the PDF export of
  a whole transcript, rebuilding the search index (Settings → Search), moving
  the workspaces (Settings → Storage).
- **In the editor**: separate bars for "re-transcribe section" and for the AI
  processing of the open file.

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
  overlap) so even local models with small context windows work cleanly — a
  two-hour recording makes about 17 requests, whose results Verba joins again
- **Nothing is shortened.** Verba sends no upper limit for the answer length;
  the model answers as far as its context window reaches. If an answer is cut
  off mid-text anyway, Verba halves the piece and asks again instead of storing
  a shortened transcript. Only when even a short piece no longer fits does the
  step end with an error
- Results appear as tabs in the AI dialog and in the editor, and are also
  written as Markdown files into the workspace under `transcripts/`

**What is running.** The dialog closes on start — progress then shows in the file
row and names the step (e.g. "AI processing · Cleanup 2/5"). Finished steps are
marked in the row as **cleaned** and **translated**, so it is visible whether a
file already went through the AI step. A second click does not queue the same
step twice; for translations the language counts — a second target language gets
its own run, and a translation also starts while the cleanup of the same file is
still running. If a step fails, the reason appears as a message and stays in the
file row — an empty result is never stored, because every PDF built from it
would be empty as well. The icons in the file row follow the order of the
workflow: transcribe → AI processing → editor (checking) → PDF.

**Full automation:** **"Process automatically"** is already switched on in the
transcript view for every new project (optionally with a target language); one
click switches it off again. Every finished
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
- **Which text counts in the end** is stated as a hint above the areas: as long
  as there is no cleanup, translation and PDF are built straight from the
  segments. Once a cleanup exists it is the basis for both — later changes to
  the segments only take effect after the cleanup is regenerated. Layouts with
  speakers (*dialogue*, *script*) are the exception: there the PDF is always
  built from the segments including the speakers, and the cleanup only feeds the
  translation.
- **Segments and AI text scroll together** and share one height, so a passage can
  be read next to its cleaned or translated version. The mapping is proportional
  — the cleaned text is one flowing text, not one block per segment.
- The **waveform stays at the top while scrolling** (from tablet width up), so
  playback and selection are always within reach.
- **AI texts right from the editor**: if a text is missing, "Create cleanup" or
  "Create translation" (with a language picker) builds it; if it already exists,
  **"Regenerate"** builds it again and replaces it. Progress and errors appear
  below the areas — when a step fails its reason stays put instead of nothing
  seeming to happen.
- In the translation language picker the languages are grouped into **"Already
  translated"** and **"Not translated yet"**; switching shows the matching
  version right away.
- **Switch files and export without a detour**: a picker at the top switches to
  another file of the same transcript, and next to it the PDF icon starts the
  **export** straight from the editor.
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

**Does the model fit this machine?** Next to its status, every row carries a
verdict for **this** system — Whisper always runs locally, so the numbers are
your own RAM and your own graphics memory:

- **suitable** (green) — enough free memory, the model runs comfortably
- **limited** (orange) — it runs, but with a caveat: either the memory is
  nearly full, or the model does not fit the graphics memory and therefore runs
  on the CPU (noticeably slower)
- **too large** (red) — this system does not have the memory; the hint names
  the largest model that does fit here

Hovering the verdict shows the whole sentence with the numbers (e.g. "needs
about 2.3 GB, 3.9 GB of VRAM free"). The same recommendation for the machine as
a whole stands above the list, and in the setup wizard (step 3) right below the
model field.

**When the memory really is not enough.** Verba does not crash:

- A model that fits nowhere is refused **before** loading — the job fails with
  a message, the application keeps running.
- If it does not fit the graphics memory, the GPU attempt is skipped and the
  model is loaded on the CPU straight away.
- If the graphics memory only fills up mid-transcription, Verba switches to the
  CPU and says so in the progress line.
- If the RAM is full, the job ends with a message naming the free and the
  required gigabytes.

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

**Installing llama.cpp.** The button sits under Settings → AI and in the
first-run setup's **Language model** step as soon as "Local (llama.cpp)" is
selected. Both show the same **installation log**: the detected system, the
chosen release and package, the download, unpacking, any system packages
installed on the way, and the version check — line by line, while it happens.
Switching views or reloading does not lose the log; it comes from the server.

**Which llama.cpp gets installed?** Verba fetches the official release that
matches the system: on Windows with an NVIDIA GPU the CUDA build plus the CUDA
runtime, otherwise the CPU build; on Linux and macOS the CPU build — llama.cpp
publishes no CUDA package for Linux.

Once unpacked, Verba runs `llama-server` once as a test. If a system library
is missing — on a lean Linux server usually `libgomp1`, `libstdc++6` or
`libssl3` — **Verba installs that package itself** (apt, dnf/yum, zypper,
pacman or apk) and tries again; that works as long as the service runs as root
or `sudo` is allowed without a password. If it cannot, the half installation is
removed again and the message names the exact `apt install …` command. If the
distribution itself is too old (the release needs glibc 2.34 and libstdc++ from
GCC 11, so Debian 12, Ubuntu 22.04 or newer), the message says that too —
instead of failing later during the first AI step. If a download is cut off —
models are several GB — Verba continues it where it stopped.

**An endpoint on your own machine.** If the base URL points at `localhost` or
`127.0.0.1` (typical for Ollama or LM Studio), an **estimate** appears below the
field: how much memory is free here right now and which model size is realistic
with it. Deliberately an estimate and not a verdict like the ones for Verba's own
models — which model that server loads and whether it uses the GPU is its own
decision; Verba does not manage it. For an address on the network or in the
cloud nothing is shown, because that is someone else's hardware.

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
the Qwen3 line. On top of that, **every** row carries the same verdict as the
Whisper models (suitable / limited / too large) — it applies to local models
only, an OpenAI-compatible endpoint runs on someone else's hardware and is not
rated. A model that does not fit the graphics memory stays in RAM instead of
killing the server on startup; a model that fits nowhere means the server is
not started at all, and says why. Nothing is decided automatically though — you download the
model yourself and can pick another one at any time; the **local model** field
shows which one the server uses.

**Models with reasoning.** "Thinking" models (Qwen3, DeepSeek-R1 and others) put
their reasoning in front of the answer — Verba cuts it away. If a model delivers
reasoning only, or its token budget ends the answer before it began, the step
fails with exactly that reason instead of an empty text. In LM Studio & co. it
is best to turn the thinking mode off or to pick a model without reasoning.
Errors from the endpoint (such as "model not loaded" or an exceeded context
length) are shown verbatim — and if the server loads the model only on the first
call (LM Studio does), that call can take considerably longer than the ones
after it.

**Your own models and your own directory.** **GGUF directory** sets the folder
the models live in (e.g. `F:\Models\llm`). Every `.gguf` file in it appears in
the selection and is **loaded straight from there** — nothing is copied and
nothing is downloaded twice. That is also how to use a model that is not in
the list at all: drop the file into the folder, pick it under **local
model**, done.

### Reasoning (thinking mode)

Some language models "think" before they answer — visible as `<think>` blocks,
or simply as an answer that takes its time. For Verba that buys nothing:
cleanup, translation and PDF structuring are transformations with an explicit
instruction, not problems to deliberate over. It costs twice — time, and the
token budget the answer itself needs. That is exactly where the message "the
model used up its token budget before an answer began" comes from.

Under Settings → AI processing there is **Reasoning** for this:

- **Off** (default) — Verba explicitly asks the model not to think. Noticeably
  faster and the most reliable setting.
- **Low** — a little thinking stays allowed. Worth it if you weigh the AI
  answers in search and help higher than the speed of the processing.
- **Let the model decide** — Verba does not interfere (the behaviour before
  this setting existed).

The setting applies to everything: processing, PDF structuring, the AI answers
in the search and questions about the guide. Technically Verba sends
`reasoning_effort` and — for templates that only look there —
`chat_template_kwargs`. Endpoints that do not know these fields (the OpenAI API
itself, for one) refuse them once; Verba remembers that and leaves them out
afterwards. Models without a thinking mode ignore them anyway.

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

Running text is set **justified**. Line breaks that came from the transcript
are pulled together into word spacing so a paragraph does not break in the
middle of a sentence — only stanzas keep their lines. An answer from the
language model that was not built from the transcript at all (for instance the
question to please supply a text) is discarded and exported rule-based. If the
transcript carries no text at all — a recording without speech, say — the
export fails with a message instead of producing a PDF that holds nothing but
the header line.

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

**The hit list.** Every file appears exactly once, with all of its hits below
it in timeline order — each with a timestamp; a click opens the editor exactly
there, and the audio starts at that position. Of the passage only the matches
are shown: one to three lines around each match, the search terms highlighted
inside them, everything in between elided with “…”. Emptying the search field
clears the list along with the question.

**The header is searched too.** A name, a date or an extra note lives in a
file's header, not in the spoken text — so Verba searches there as well:
title, the three header fields, recording date and file name. Such a hit
appears marked **Header** above the passages of the same file. It is matched
literally and with all search terms at once, so “Meier 2024” finds that one
file instead of every file with a 2024 somewhere in it; the date may be
written in German notation (“12.05.2024”).

With an LLM configured, **“AI answer”** sits right next to **Search** and
produces an answer that backs every statement with numbered sources. The
source list gives number, transcript, file and position only — the passage
itself stands in the answer above — and is clickable like a hit. The AI
answers only from the retrieved passages; if there are none, it says so
honestly instead of guessing.

New transcriptions and segment edits are indexed automatically; deleted files
disappear from the index immediately. **Settings → Search** shows the index
status, the embedding model and a button for a manual rebuild. The search
components are installed by the setup (feature group “Semantic search”). While
they are missing, **Start setup** in step 1 stays clickable even when
everything else is installed.

**Embedding model.** The choice is a fixed list of vetted models — all
multilingual (German queries find English and Russian content) and
CPU-friendly:

| Model | Size | Languages | Character |
| --- | --- | --- | --- |
| MiniLM multilingual (default) | approx. 0.5 GB | 50 | fast |
| Multilingual E5 small | approx. 0.5 GB | 100 | balanced, a bit more precise |
| mpnet multilingual | approx. 1.0 GB | 50 | thorough, slower |
| BGE-M3 | approx. 2.3 GB | 100 | best quality, noticeably slower |

**Does the model fit this machine?** As with the Whisper models, every entry
carries a verdict for this system (suitable / limited / too large), and the full
sentence with the numbers stands below the picker. Only the **RAM** is counted —
the search always encodes on the CPU, so the graphics memory plays no part here.
If the chosen model does not fit, the message names one that does; and a model
that cannot fit is refused before loading instead of letting the index run fail
mid-allocation.

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
- **Storage & logs:** data directory, workspace directory, server port, log
  level and retention (older logs are deleted automatically). The **data
  directory** holds the database and the logs — the part that belongs in a
  backup, which is exactly what pointing it at a backed-up drive is for. The
  settings, the downloaded ffmpeg and the model directories stay with the
  installation, because all of those can be fetched again. A new path is
  saved like any other setting, but the move itself happens at the **next
  start**: the database and the log files are open while the app runs and
  cannot be pulled out from under it. Until then the line below the field
  says where the move goes, a reminder stays on the start page, and everything
  keeps working in the old location. Transcript folders sitting at their
  default place inside the data directory move along with it.
- **Database size:** deleting a transcript frees pages inside the database but
  does not shrink the file — the space is reused by the next write. Once
  enough of it is free, Verba compacts the database on its own: as the
  background step "Compact database" after a deletion, and at the next start.
  Small remainders are left alone, because rewriting the whole file for them
  would not pay off.
- **Search:** index status, embedding model (pick list), models directory,
  rebuild index
- **API:** keys for the public transcription API (section "Public API")
- **My account:** change your own password, delete your own account and —
  as an administrator — the way into the user management (section
  “Users & visibility”)
- **System:** information about the machine Verba runs on — CPU (model and
  cores), memory (free/total), graphics card with VRAM, ffmpeg status — plus
  the app version

## Users & visibility {#security}

By default Verba is **unprotected**: whoever reaches the address sees
everything and may do everything. For local use on your own machine that is
exactly right. As soon as Verba runs on a server, or several people work with
it, switch the **user management** on.

### Switching it on

During the first-run setup in the **Access** step, or later at any time under
Settings → My account → **Open user management**. You create an administrator
account there; from that moment the application is protected and only
reachable after signing in.

Nothing is lost by switching it on: all existing transcripts stay as they
were, stay **public** — visible to every signed-in user — and are assigned to
the first administrator as their owner. They can hand them over to other
people or change their visibility afterwards.

If you skip the step no account is created and Verba stays open. The
application says so plainly at that point.

### Roles

- **Administrator:** manages users, settings, Whisper and language models,
  transcript types, the search index and API keys. Sees and edits every
  transcript.
- **User:** works with their own transcripts and the ones shared with them,
  searches and exports them. In the settings they keep the interface
  language, the documentation and their own account.

The first user is always an administrator. The **last** administrator account
can neither be deleted nor demoted to a normal user — otherwise nobody would
be left who could manage users or settings.

### Creating accounts

There is no self-registration: an administrator creates accounts under
Settings → User management, with a start password. At the first sign-in the
user has to choose their own password — until then they get no further. The
same applies again when an administrator resets a password.

### Visibility per transcript

Every transcript has one of three visibilities. It appears as a coloured badge
on the transcript card and is changed through the padlock icon:

- **Private** — only the owner and administrators
- **Shared** — plus the people explicitly selected
- **Public** — every signed-in user

**Whoever can see a transcript may also edit and delete it.** The only
exception is the visibility and the share list themselves: only the owner and
administrators change those. Otherwise a colleague could set a public
transcript to private and lock everyone else out.

Which visibility new transcripts get is set by an administrator under User
management → **Default visibility**; it can be overridden at creation time.

Visibility applies everywhere, not only in the overview: the search finds only
what you may see, the status line never names somebody else's file, and the
files, segments and PDF exports of a foreign private transcript are out of
reach.

### Your own account

Under Settings → **My account** you change your password (which signs out
every other device) or delete your account.

When an account is deleted:

- **Private** transcripts are deleted together with their audio files — they
  belonged to that one person alone.
- **Shared and public** transcripts are kept and handed to the
  longest-serving administrator. They are other people's working material and
  must not disappear from under them.

The same happens when an administrator deletes somebody else's account. The
application names the number of affected transcripts before deleting.

### What else changes

- The **public API** (`/v1`) always requires an API key while the user
  management is on — otherwise the sign-in could simply be bypassed there.
- Signing in uses a session cookie. Passwords are stored hashed with scrypt,
  sessions only as a checksum; neither is in the database in clear text.
- **A reverse proxy terminating TLS** is the normal setup and works: Verba
  takes the browser's scheme from `X-Forwarded-Proto` and marks the cookie
  `Secure` from it — the hop from the proxy to Verba may stay plain HTTP.
  The header is accepted from `127.0.0.1` only; with the proxy on another
  machine, start Verba with `FORWARDED_ALLOW_IPS=<proxy ip>` or set
  `auth.cookie_secure` to `always` in the settings.

### Switching it off again — and back on

Under User management → **Disable user management**. Verba is then open again
to anyone who reaches the address. What happens:

- Every active sign-in ends immediately.
- The accounts are kept, with their passwords and roles.
- Owners, visibilities and share lists stay in the database — they are only
  no longer enforced. A private transcript is reachable by anyone again, but
  it stays stored as private.
- The public API (`/v1`) falls back to its old rule: open as long as no API
  key exists.

The same button switches it back on — it then reads **Switch user management
back on** and asks for nothing: no second administrator account is created,
everybody signs in with the password they already had, and owners and
visibilities apply again as before. Transcripts created while it was off have
no owner; they go to the longest-serving administrator and stay public, so
they lock nobody out.

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

**API keys:** Keys can be created and deleted in the settings under "API".
The **label** is required — it is the only thing a key is later recognised by,
so the create button stays inactive until the field holds something. As
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
