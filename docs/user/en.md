# Verba — User Guide

Verba turns audio recordings into editable text — fully local, with optional
AI cleanup/translation, PDF export, semantic search and a public
transcription API.

## Installation & starting {#install}

**Ready-made packages** (project releases page):

- **Windows:** `Verba-Setup-….exe` — double-click, wizard, start menu entry.
  Verba installs an update itself (see *Updating*); running the newer
  installer over the existing installation still works. Data is stored per
  user and survives updates.
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

Verba gives the memory of the local models back when it ends: the local
language model server is stopped, the Whisper model and the model behind the
search are unloaded. Once the window is closed, no model is left in RAM or in
the graphics memory. A hard end (Task Manager, a power cut) leaves no
opportunity for that — on Windows Verba takes the AI server with it anyway,
and one that is left behind regardless is stopped at the next start.

Verba is a **PWA**: the browser can "install" the app (icon in the address bar
or "Add to home screen"), after which it feels like a standalone app. The
interface also loads without a connection; as soon as the server is reachable
again, everything resumes automatically.

## Updating {#updates}

Verba looks for a newer version once a day and then says so: as a short
message while the app is open, and as a reminder on the start page. Only an
administrator can install it.

**Settings → System** shows the running version at the top, with two round
buttons next to it: the circular arrow looks for a new version right away
instead of waiting for the daily check, and the downward arrow installs it. If
a newer version exists that button is active and highlighted, and its tooltip
names it ("Update to 0.1.2"); if the running version is the newest, it stays
inactive. The line below says in words what currently holds.

One click downloads the package that belongs to this installation and installs
it. What happens is written line by line into the update log below the button:
download, verification, installation, restart. The log is only there while the
installation runs — after the restart the row above shows the new version
number, and that is the answer.

- **Windows:** the installer runs without a wizard. Windows asks for
  permission once — that is the only click left. Verba then closes, the
  installer replaces the files and starts Verba again.
- **Linux desktop:** the running AppImage file is replaced by the new one and
  relaunched.
- **Linux server:** Verba updates the dependencies, replaces the application
  files and restarts the service.
- **Source installation:** `git pull` updates this one. The button stays
  inactive and names the reason.

The old version is removed on the way — it is not needed once the new one
runs, and nothing that matters is part of it: database, logs, settings,
workspaces and downloaded models all live outside the program files and stay
untouched. If Verba on a server does not run as a service, the log ends by
saying that a restart is needed.

The check asks GitHub for the newest release. If that is not wanted, switch
**Look for new versions automatically** off in Settings → System; no request
then leaves the machine for it, and updates go through the project's releases
page.

### The server's system packages

When Verba runs on a Linux server, a second row sits below the version row:
**Linux server**, with one round button that updates the machine's system
packages — `apt update` and `apt upgrade`, exactly what one would otherwise
type in over SSH. On Windows and on the desktop that row does not exist.

One click starts both and shows the server log below it: every line apt
prints, as it appears. The log is not stored — it is the recording of this one
run. Verba itself is left alone, and the server never restarts on its own: if
a package needs a restart, the log says so at the end.

Below that sits **Full upgrade (dist-upgrade and autoremove)**. Ticked, it
runs `apt dist-upgrade` instead of `apt upgrade` — which also installs
packages whose dependencies changed, and may remove packages on the way — and
then `apt autoremove`, which clears out what nothing needs any more. That is
the thorough variant, and therefore one to choose deliberately: without the
tick the server's set of packages stays as it is. The choice applies to this
one run and is not remembered; the button's tooltip says which of the two it
starts.

Installing packages needs the rights for it: the service runs as root or may
use `sudo` without a password. Without that the button stays inactive and the
line below names the reason.

**Enabling the button (for the system administrator).** With the shipped unit
`deploy/verba.service` it is inactive out of the box, and deliberately so.
Having *installed* Verba as root does not mean it *runs* as root:
`deploy/install.sh` creates the system user `verba`, and three of the unit's
lines deny the service root in every direction — `User=verba`,
`NoNewPrivileges=true` (`sudo` is setuid, so this switches it off) and
`ProtectSystem=full` (`/usr` and `/etc` are read-only, so dpkg would not get
through even as root).

Two steps on the server enable it. First, `sudo` without a password for the
service user, narrowly limited to `apt-get` and to `true` — Verba probes the
rights with `sudo -n true` without doing anything, so that has to be allowed as
well:

```bash
printf 'verba ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/true, /bin/true\n' \
    | sudo tee /etc/sudoers.d/verba-apt >/dev/null
sudo chmod 440 /etc/sudoers.d/verba-apt
sudo visudo -c
```

Second, relax the two hardening lines that stand in apt's way, via `sudo
systemctl edit verba`:

```
[Service]
NoNewPrivileges=false
ProtectSystem=no
```

After `sudo systemctl restart verba` the button is active; the page asks for
the state on every visit. If it stays inactive, `sudo -u verba sudo -n true`
(exit code 0?) says whether the sudoers rule applies, and `systemctl show verba
-p NoNewPrivileges -p ProtectSystem` whether the override arrived.

Leaving the button inactive is a valid choice: `apt upgrade` over SSH does the
same thing, and Verba then needs no more rights than it otherwise has.

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
`20240817_Title.mp3`). **The date in the file name wins**: tags only supply the
date the name does not state. Otherwise an m4a/MP4 would be dated by its
container timestamp `creation_time`, which says when the file was last written
(copied off the recorder, converted) rather than when it was recorded.

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

There is also **The export reproduces the text unchanged**, on for every type
but the minutes one. The PDF export then structures the processed text and the
translations by itself — without an AI step. Every sentence reaches the PDF
unchanged and in its original order, paragraphs stay where the text puts
them, and no heading appears that the text does not carry. The output-format
prompt is then unused.

Without this option the AI may rebuild the text **for the export** according
to the output-format prompt. Only a type needs that which turns its material
into something else at export time. If an export is caught dropping or
rewording sentences, switch the option on. What the AI processing does with
the transcript is not this option's business.

That is what the option below decides: **The AI processing writes a document of
its own**, off for every built-in type but **Meeting Protocol**. With it off,
the AI cleans section by section — spelling, punctuation, filler words and
false starts —, every sentence comes back, and the cleanup prompt only tells
the AI what kind of text it is looking at. With it on, the cleanup prompt is
the **instruction** and runs exactly **once over the whole recording**: that is
how minutes come out with one title, one list of decisions and one to-do list
instead of one set per section. How Verba keeps the whole recording in view for
that is described under "AI processing".

The two options are **independent**: a type may have a document of its own
written from the recording and still have that document reach the PDF word for
word — which is the usual choice when the processing should work but the export
must not touch anything any more.

Next to it sits **Do not split sections across pages**. When a whole
transcript is exported, one file otherwise follows the next on the same page,
and a section breaks wherever the page happens to end. With this option a
file's section starts on a new page as soon as it no longer fits on the
current one — counted with everything that belongs to it: the header, the
text, and the translations that follow the original in the same PDF. So if
only half of B fits, or B fits but its translation does not, B starts at the
top of the next page. Sections that do fit are not moved: several short
recordings still share a page. And a section longer than a page gets no empty
page in front of it — it fits nowhere completely and runs over as before. The
option belongs to the type, so it only applies to the transcripts using it,
and it is off for every built-in type.

There is also **Recognise speakers automatically**, on for
**Interview/Dialogue**, **Meeting Protocol** and **Roleplay** and off for the
single-voice types. With it on, the transcription is followed by working out
who speaks when, and a segment holding a speaker change is split at that
moment — details in the section "Speaker recognition".

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

How much hangs off a type is shown in the list as a marker and in the editor
as a line above the fields: how many transcripts have it as their rule and how
many files picked it for themselves. **Deleting** stays possible at any time,
and it takes none of the work with it — cleaned texts, translations and
finished PDFs stay as they are. It only changes what future runs follow: the
transcripts affected are left without a type, so they work without the cleanup
and the output prompt, without speaker recognition and with the text taken
into the export unchanged. The files affected only lose their exception and
follow the type of their transcript again — which may well be a different one.
The question before the deletion names what is affected.

### A type of its own for a single file

A transcript's type applies to everything in it — but a transcript holds what
was recorded, and that is not always of one kind: the songs of a rehearsal
next to the conversation about them. So **every file can be given a type of
its own**, which then applies to it.

In the file list a **chip** next to language and duration says which type the
file is processed as. It is outlined and italic while the file follows the
transcript, and filled as soon as it names a type of its own. A click opens
the picker: **From the transcript** puts it back under the global type, any
other entry sets its own.

The file's type then decides everything a type decides: the cleanup prompt of
the AI processing and how it runs, whether the export reproduces the text
unchanged, the layout of the
PDF export and the page break in a compilation export. One compilation PDF can
therefore hold a song in stanzas and an interview with speakers — every file
in its own shape. In the search, the type filter matches the type that applies
to the file.

In the **transcript overview** every type of a transcript stands side by side:
the transcript's own type filled, and behind it, outlined, the types single
files set for themselves. When a type is deleted, the files that named it fall
back to the transcript's type.

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

**Every file is a card.** The file list is a list of cards. The top line
carries the selection checkbox, the file name and — at the far right — the
three-dot menu; the line below it carries the language and the running time on
the left and the four round step badges at the right edge of the card. The
badges sit one line lower for a reason: the stop button comes and goes in the
corner above them, and the badges must not jump around when it does.
**Clicking the card opens the editor**; everything else on it (checkbox,
language chip, stop button, menu) does only what it says. The **right mouse
button** anywhere on the card opens the same menu where the pointer stands —
in a long list that is a good deal closer than the corner.

**Several files at once.** Every card carries a checkbox in its top left
corner, and the *Select all* line above the list selects them all. As soon as something is
selected, a bar appears above the list with the count and three actions for
exactly that selection: **Transcribe**, **Export as PDF** (a single PDF in
which every selected file is one section — like the export of the whole
transcript, just without the files that were not selected; files without a
transcript are skipped and the bar says how many) and **Delete**. The selection survives while jobs run and the cards update.

**The four step badges.** On the right of every card stand four round badges
— **transcription**, **cleanup**, **translation**, **search index** — always
all of them, even for a step that has not run. That is the point: the card
says what is still missing as clearly as what is done.

| Look | Meaning |
| --- | --- |
| greyed out | not run yet |
| grey ring, pulsing | waiting in the queue |
| ring filling with blue | running — the filled share is the progress |
| green | completed |
| red | failed (the reason stands as text on the card) |

The words are in the **tooltip**: hovering it (or tapping it on a phone) names
the step and, while it runs, the percentage and the sub-step — e.g.
`Aufbereitung — 40 % · Bereinigung 2/5` or
`Transkription — wartet (Position 3)`.

The last badge, the **magnifier**, is the search index. Verba builds it by
itself — after the transcription and after every change to the text — so there
is nothing to start here. Grey means this file cannot be found through
**Search** yet; green means it can.

**The actions live in the three-dot menu** on the right of the card — and in
the menu the right mouse button opens, which shows the same list —, each with
an icon and a label: *Transcribe*, *AI processing*,
*Open in the editor*, *Export as PDF*, *Delete*. While a step runs, a **stop
button** sits next to it directly on the card — cancelling is the one thing
that is urgent then.

## Transcribing {#transcribe}

- **Single file:** *Transcribe* in the three-dot menu of the card — on a file
  that is already finished this is the second run, and it replaces its
  segments
- **Everything:** "Transcribe all" in step 2 of the action card — the button
  takes the files that are still open. Once they are all transcribed it offers a
  second run over the whole list and asks first, because that replaces the
  existing segments, manually edited ones included. The same question comes up
  when a selection contains finished files.
- **Advanced (expandable):** change the Whisper model and recording language
  for this run only — saved settings stay unchanged
- Progress appears live in the step badge of the card; a running job is
  cancelled with the stop button on the card
- **Where the language comes from** — in this order, the first one given
  wins: the language under *Advanced* for this single run, then the language of
  the file (from the file name, editable in the editor), then the language from
  the settings. Only when none of them says anything does Whisper detect the
  language itself. A language that was given is **not overwritten** by detection
  either; without one, the detected language is written to the file.
- **A language is shown as a chip** — with its country's flag, the ISO code and
  the full name in the tooltip; where no language is set the chip reads "auto".
  A click opens the language picker with a filter field: typing narrows it and
  Enter takes the first match. That is how the language appears on the card of
  the file, below the waveform in the editor and in the tool row
  of the workspace for the translation. Where the operating system draws no flags — Windows does not —
  the country letters stand in the flag's box; a language without a single
  country behind it (Catalan, Basque, Latin, …) gets a globe.
- Tip: stating the recording language explicitly (instead of auto-detect)
  noticeably improves results — and keeps a misdetected language from carrying
  on through cleanup and translation

**Where the progress is shown.** Every step reports which file it is working
on and how far it has got:

- **On the card of the file** (transcript view): in the step badge, whose ring
  fills up, with the text in the tooltip — e.g.
  `Transkription — 40 % · 01:23` while transcribing (percent = position in
  the recording), `Aufbereitung — 40 % · Bereinigung 2/5`,
  `Übersetzung — 70 % · Übersetzung 1/3` and
  `Transkription — wartet (Position 3)` while waiting. "Transcribe all" and
  "AI processing (all)" create one job **per file**, so every card shows its
  own progress.
- **In the top bar**: a summary that keeps running while you switch views —
  `KI-Aufbereitung — lied.mp3: Bereinigung 2/5 · 40 %`. With two jobs in
  parallel (transcription and processing) both are named, further ones as
  `+2 more`.
- **In the transcript list**: a `3 running` marker on a transcript while
  something is running there.
- **As its own card**: jobs that belong to no single file — the PDF export of
  a whole transcript, rebuilding the search index (Settings → Search), moving
  the workspaces (Settings → Storage).
- **In the editor**: separate bars for transcribing the open file (the whole
  file as well as a selection) and for its AI processing.

**Queue:** all jobs run through one central queue so the hardware is never
oversubscribed — even with several people working at once. Waiting files show
their queue position; small jobs (transcribing a selection, audio edits) jump
ahead, and the order stays fair per user.

## Speaker recognition {#speakers}

Whisper hears words, not people. It ends a segment where there is a pause in
the speech — and in a conversation that pause regularly does **not** coincide
with the change of speaker. A segment then holds the end of one sentence and
the beginning of the reply, and the transcript reads as a single voice
although two people were talking.

Speaker recognition is the second pass that takes this apart:

- Every segment gets its speaker — **Speaker 1**, **Speaker 2**, …, numbered
  by who talks first.
- **A segment in which the speaker changes is split at that moment.** One
  segment with two voices becomes two segments with one each.

The names are placeholders: the recognition can hear that two different people
are talking, but not what they are called. Renaming one asks whether the other
segments of the same person should follow — answering once instead of editing
two hundred rows by hand.

### Where it is switched on

Whether the recognition runs automatically after every transcription is
decided by the **transcript type** (section "Transcript types") with the
switch **Recognise speakers automatically**. It is on for
**Interview/Dialogue**, **Meeting Protocol** and **Roleplay**, and off for
**Song**, **Poem** and **Speech**: with a single voice, every recognised
"second speaker" would be an error.

Independently of that, the **Recognise speakers** button in the **editor**
starts the recognition for the open recording — even for a type that does not
ask for it. That is the way in for the one recording that turns out to be a
conversation.

### Number of speakers

How many people can be heard is always worked out by the recognition itself —
the number is never stated anywhere. What can be turned is the **separation
threshold** in the settings: it says how different two voices have to sound to
count as two people. If two similar voices are merged into one, a lower value
helps; if speakers appear that do not exist, a higher one.

### Setting it up

The recognition needs a component of its own and two model files (about 35 MB
together), both in **Settings → Transcription → Speaker recognition**:

- **The component first**, then the models. It is installed along with the
  first-run setup; if it is missing, the page says so, names the package that
  is absent, and a button next to it installs the component on the spot, with
  the progress below. Only then do the models help: they can be downloaded
  first, but without the component the button in the editor refuses even
  though the models say "installed".
- The **segmentation model** finds the stretches of speech and has no
  alternative. The **speaker model** recognises the voices again; here size is
  a choice: the default model is enough for clearly different voices, the
  larger ones help with similar voices, many speakers or poor recording
  quality. The language of the recording does not matter — a voice is a voice.

Everything computes on the processor, without a graphics card, without an
account and without an internet connection once the models are downloaded. An
hour of audio takes a few minutes, depending on the machine.

### When the cut is exact — and when it is a guess

When the recognition follows a transcription, Whisper has additionally
recorded **when each word was said**. The cut then falls exactly between two
words and not a character is lost.

For a transcript made without those timings — an old recording, or one whose
text was edited by hand — the position in the text is **estimated**: from the
share of the duration that has passed up to the speaker change, and then moved
to the nearest sentence end. Where the speaker changes at the end of a
sentence — the normal case in a conversation — that is exact; where it changes
mid-sentence, half a clause can end up on the wrong side. Whoever needs it
exact transcribes the file again and runs the recognition afterwards.

Changes too short to be turns are deliberately ignored: a single word that the
recognition attributes to the other person, in the middle of a sentence of the
same voice, is the recognition wobbling and not a change of speaker — no
sentence is cut in two for that.

### What happens with it afterwards

- The **Dialogue** and **Script** layouts build the PDF from exactly these
  segments including the speaker names (section "PDF export").
- The **search** can be narrowed to one speaker (section "Search").
- The **AI processing** and the search index wait for the recognition: they
  read the segments it is about to rewrite.

## AI processing (cleanup & translation) {#ai}

Once a language model is configured (section "Setting up a language model
(LLM)"), transcribed files get a
an **AI processing** entry in the three-dot menu of their card, and step 2 of
the action card gains **AI processing (all)**:

- **Cleanup** removes filler words and false starts, fixes punctuation and
  obvious mishearings — the transcript type flows in as context, and every
  sentence comes back. Turning the recording into something else takes a type
  with **The AI processing writes a document of its own** ("minutes" then get a
  summary with decisions and a to-do list)
- **Translate** renders the cleaned text (or the raw transcript) into nearly
  any language — all ~100 languages Whisper knows are available as targets. The
  translation carries the **register and subject matter of the original** over:
  a sermon, a prophecy or a liturgical text comes back in the established
  biblical wording of the target language ("thus says the Holy Spirit" rather
  than a plain paraphrase; "walk before God" rather than "go before God"), a
  specialist text in the settled terminology of its field — not in everyday
  language. As with the cleanup, the **transcript type flows in as context**:
  what its prompt says about the kind of text also steers the wording of the
  translation
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

**The whole text in view.** Before a step runs, Verba reads the recording once
for an overview: every section is condensed into a short digest (topic, names
and spellings, decisions, tasks, verbatim quotations), and from those digests
comes a picture of the **whole** recording — title, summary, sequence of
topics. Where there are more digests than fit into one request, neighbouring
ones are condensed further until everything fits; no request ever runs past
the model's context window. That overview serves two purposes:

- For **cleanup and translation** it is orientation: names, technical terms
  and spellings stay the same across section boundaries. The text itself is
  still worked on section by section — every sentence has to come back. The
  model is shown **only the title and the spellings**, never the summary:
  nothing in the input of a step that must hand back every sentence should
  read like a shorter version of the text. If a model condenses a section
  anyway, Verba notices by how little of the section is left, asks for that
  same section again without any orientation — and ends the step with a
  message rather than storing a shortened version
- For transcript types that turn the material into **something else** (a type
  with "The AI processing writes a document of its own", e.g. "Meeting
  Protocol"), the type's prompt
  runs **once over the whole recording** instead of once per section. That
  yields minutes with one title, one list of decisions and one to-do list —
  instead of one separate set per section

The overview is built once per file and shared by every step (cleanup,
translation, PDF export). If the transcript is edited in the editor, Verba
reads it again for the next step.

**A title from the recording.** The title Verba forms over the whole recording
is given to files whose name states no title of its own (`meeting.m4a`,
`REC_0042.wav`) or only a date (`20260304.m4a`) — as the title and as the
header title for the PDF export. A
title from the file-name scheme, from an audio tag, or a header line edited by
hand is left untouched.

**What is running.** The dialog closes on start — progress then shows in the file
row and names the step (e.g. "AI processing · Overview 2/5", then "Cleanup 2/5"). Finished steps are
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
pencil icon ("Open in editor") on a transcribed file opens the editor — a
**workspace** for audio,
transcript and AI texts:

- **Waveform** with play/pause; clicking a segment timestamp jumps the audio
  there; during playback the active segment is highlighted — the list does not
  scroll along, it stays where it was left. Only a hit opened from the search is
  brought into view once, on arrival.
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
- The translation's language chip sits at the top in the tool row of the
  workspace — next to the spell checker, together with the bin — and only
  appears while the translation is open. It opens a picker in which the
  languages are grouped into **"Already translated"** and **"Not translated
  yet"**; switching shows the matching version right away.
- **Deleting a translation**: next to the language chip the bin icon removes
  the shown translation for good after a confirmation — including its file in
  the workspace. The language moves back under "Not translated yet", the PDF
  export no longer offers it, and the *translated* mark disappears from the file
  row. Clearing the text box alone achieves the same for the mark and the
  export; the empty entry itself stays until it is deleted.
- **Switch files and export without a detour**: a picker at the top lists every
  file of the same transcript and shows the open one as selected — one pick
  moves to another; next to it the PDF icon starts the **export** straight from
  the editor. Once the PDF is finished the browser downloads it right away —
  the editor has no list of exports to pick it from. It still lands in the
  `exports/` folder and in the transcript's **Exports (PDF)** section as always.
- **Recognise speakers**: the two-people button under the waveform starts the
  speaker recognition for this recording — even when its type does not ask for
  it. How many people are talking is worked out on its own. Existing speaker
  names are replaced, and you are asked before that happens. See the section
  "Speaker recognition".
- **Text and speaker** are edited directly in the segment rows — changes are
  saved automatically ("Saved" indicator), **Undo** reverts recent changes
  step by step
- **Spell checking**: segments, cleaned text and translation are checked by the
  browser's own spell checker — each text **in its own language**: segments and
  cleanup in the language of the transcription, every translation in its target
  language. Speaker names are exempt, otherwise every name would be underlined.
  The icon at the top of the workspace switches the check off and on again; the
  choice is remembered in the browser. Only what the browser has a dictionary
  for is marked — if one is missing for a language, it has to be added in the
  browser settings (Chrome/Edge: Languages → Spell check).
- **Language of the recording**: below the waveform a chip states the language
  the file is spoken in. Otherwise Whisper detects it itself — and does get it wrong;
  from there the whole chain runs in the wrong language: the transcript, the
  cleanup, and a "translation" that ends up labelled with a language it is not
  in. Set here, the language applies to **every further transcription of this
  file** — the whole file as well as a selection — and detection no longer
  overwrites it. "Detect automatically" hands the decision back to Whisper. It
  is prefilled from the file name (`20260731_ru_de_…` means Russian).
  Together with the spell checking that means: after switching, the browser
  checks segments and cleaned text in the new language right away.
- **Re-transcribe the whole file** — the button with the waveform turning into
  lines of text, usable
  without a selection: the file is recognised again completely and **every
  segment is replaced**, including your own edits to text and speakers (hence
  the confirmation). This is the way out after the language or the model has
  been corrected. Cleanup and translations stay as they are until they are
  created again via "Recreate". The progress runs below the waveform.
- **Selection** by dragging on the waveform. **Several passages** can be
  selected:
  - **Shift+drag** adds another passage to the selection; a drag without Shift
    starts a new one.
  - The **right mouse button** opens a menu where the pointer stands, with
    exactly the actions the toolbar offers right now. With several passages
    selected two more join them, for the passage under the pointer alone:
    playing just that one, and *Deselect this passage*.
    the cross clears the whole selection.
  - Passages that overlap are merged into one — so one can drag on without
    minding the gaps. Below the waveform stands how many passages are selected
    and how much time they add up to.
- **Play plays the selection** — and nothing else: from the first passage on,
  the gaps in between are skipped, and it stops at the end of the last one. A
  **click on a selected passage** plays exactly that one. With nothing selected
  the whole file runs, and with cuts marked (see below) that is how the result
  can be listened to before it is written.
- With a selection:
  - **Transcribe selection** — only the selected passages are recognised, and
    the result is **plain text**: a list below the buttons, one entry per
    passage, in the order of the recording and with its start time. For a single
    passage the text goes to the clipboard as well; if the browser does not
    allow that by itself (it usually wants a click for it), the *Copy* button on
    each entry or *Copy everything* at the top does it. **No segments are
    created** — the existing ones stay untouched. That way one can listen back
    to what was really said in a passage without overwriting one's own
    correction; for replacing the segments there is "Re-transcribe the whole
    file". Should a recognised text be worth keeping after all, the **plus** in
    its row files it as a new segment for exactly that passage. The start time
    in the row plays the passage again.
  - **Add empty segments for the selection** — the plus button: for every
    selected passage a segment without text is created, ready to type into, and
    the cursor lands in the first of them. It is meant for passages the
    recognition skipped. The segments take their place in the list by their
    start time, not at the end of it.

**Keyboard shortcuts.** Every one of these buttons has a key, and the key is
named in its tooltip. They apply while the cursor is not in a text field —
when typing, every key belongs to the text — and they do exactly what the
button does right now: a greyed-out button does not answer its key either.

| Key | Action |
| --- | --- |
| Space | Play / pause |
| `R` | Re-transcribe the whole file |
| `T` | Transcribe the selection |
| `N` | Create empty segments for the selection |
| `K` | Trim to the selection (marked, not applied) |
| `X` | Remove the selection (marked, not applied) |
| `Z` | One step back (the last mark) |
| `Esc` | Clear the selection |

**Cutting — the way an audio editor does it.** The recording itself is changed,
and it is the open file that changes: **no second file** appears per cut. So
that this does not happen by surprise, cuts are **marked** first and applied in
one go afterwards:

- **Trim to selection** keeps only the selected passages, **Remove selection**
  takes them out. Neither changes the file yet — it marks the cut. What would go
  is **marked red** on the waveform and still audible.
- Both can be used repeatedly and in any combination; every step works on what
  the one before it left. Above the segment list stands the balance: how many
  cuts are marked, how much goes, and how long the recording will be afterwards.
- **Step back** takes the last cut out again, **Discard** takes out all of them.
  As long as nothing is applied the file is untouched — and the marks survive an
  accidental reload of the page.
- **Apply** writes them into the file after a confirmation, in a single ffmpeg
  pass. The **segments move with it**: their timestamps are recalculated, and
  segments lying entirely in what is removed are deleted. The waveform, the
  duration, the transcript file in the workspace and the search index are up to
  date afterwards.
- **The original stays backed up.** Before the first apply, Verba puts the
  untouched recording aside once — together with the transcript that belonged to
  it. **Restore the original** (the clock button, which only appears then)
  brings both back. One copy per edited recording, not per cut; whoever values
  the disk space more switches the backup off under *Settings → Storage* — an
  applied cut is then final.
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
  **It is read by position, not by counting fields** — every field after the
  date may be missing:
  - The date comes first, as `YYYYMMDD` or `YYYY-MM-DD`.
  - Then up to two language fields. Only a code Whisper knows (`de`, `ru`, …)
    or an empty field for "not given" counts as one: `20260731_ru__Title`
    means source Russian, no translation. A title is therefore never mistaken
    for a language.
  - What is left is the **title**. Only when another field follows it is the
    last one the **addition**. So `20260731_ru_de_Wesner Ronald` gives the
    title "Wesner Ronald", while `20260731_ru_de_Wesner_Ronald` gives the title
    "Wesner" with the addition "Ronald".
  - The scheme has no field for a **place**; it goes into the editor's free
    *place and date on the right* field (e.g. `Bremen-Mahndorf, 2026-07-31`).

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

**Actually using the GPU (CUDA).** A detected graphics card is not enough on
its own: Whisper computes through CTranslate2, which needs two NVIDIA
libraries that no Python package brings along — cuBLAS 12 and cuDNN 9. Without
them the transcription runs on the CPU even though the settings say GPU.

- Verba checks this and shows the result under **Settings → System** in the
  row **GPU acceleration (CUDA)**; the setup wizard lists the same row among
  its components.
- If the libraries are missing, the wizard installs them; on an existing
  installation the button next to that row is enough. The download is about
  1 GB, is only offered on machines with an NVIDIA card whose transcription is
  not set to "CPU", and takes effect without a restart.
- If the card itself does not answer — the row says so — no download helps,
  the driver is what is missing. In a container (Proxmox LXC, Docker) it is
  usually the devices `/dev/nvidia-uvm` and `/dev/nvidia-uvm-tools` that are
  not passed through; `nvidia-smi` still lists the card. The driver version
  inside the container also has to match the host's.

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

**Which llama.cpp gets installed?** One that actually uses the graphics
card. Verba tries in order and **checks after every step whether the installed
`llama-server` really sees the card** — a build that does not find it would
otherwise compute in main memory unnoticed:

1. **The platform's GPU package.** On Windows the CUDA build plus the CUDA
   runtime. On Linux the Vulkan build — llama.cpp publishes no CUDA packages
   for Linux, and Vulkan reaches an NVIDIA card through the driver's ICD for a
   30 MB download. If the Vulkan loader is missing, Verba installs it
   (`libvulkan1`).
2. **Building it here.** If no prebuilt package finds the card, Verba fetches
   the source of the same release and compiles `llama-server` with CUDA on
   this machine. It installs the tools it needs first (`cmake`, `g++`, the CUDA
   toolkit) — the toolkit is several GB, the build takes 10 to 20 minutes, and
   both are in the log. It builds for the installed card only, and the number
   of parallel compilers follows the available memory rather than the core
   count. Linux only; Windows has a CUDA package anyway.
3. **The CPU build.** What is left when nothing reaches the card — the
   language model is then slower, but it runs.

Next to "installed" you then see **which** build it became: `CUDA`, `Vulkan`,
`CUDA (built here)` or `CPU`. Hovering it names the card the build reported.
Where the card itself does not answer, nothing is built either — in a
container the devices `/dev/nvidia-uvm` and `/dev/nvidia-uvm-tools` are
usually missing, and the log says so.

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

**Already installed without GPU support?** The ladder only runs when nothing
is installed — an existing installation is never replaced behind your back.
Verba does measure it once, though: the first time the settings are opened the
installed `llama-server` is asked which devices it sees, and the answer then
sits as a badge next to "installed". If it says `CPU` while there is a card in
the machine, **"Install the GPU build"** appears next to it: one click walks
the ladder again. The existing installation stays in place until a
replacement has proven itself — if the attempt fails, because the server
cannot reach the network for instance, what ran before keeps running. The
downloaded language models are left untouched either way.

**Removing it again.** Next to the row there is **"Remove"** — that deletes
the llama.cpp installation (after a confirmation), not the models. GGUF files
are deleted one by one with the bin icon in their row; that includes models
which did not come from the catalog but were placed in the GGUF directory by
hand — they show up below the catalog with their file name and size.

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

The export runs in two stages, and how the first one works is the transcript
type's choice via **The export reproduces the text unchanged** (see
"Transcript types").
With the option on — every builtin type but the minutes one — Verba structures
the cleaned text and the translations by itself: word for word, in the order
of the text, without AI. With it off the AI structures the text according to
the type's output-format prompt (minutes with a summary and to-dos, say);
without an LLM the structure is rule-based even then — the export always
works. The layout follows the type, e.g. invisible separators and
extra whitespace or a script layout for role plays.
Without a type a plain text PDF is produced.

Running text is set **justified**. Line breaks that came from the transcript
are pulled together into word spacing so a paragraph does not break in the
middle of a sentence — only stanzas keep their lines. Where a type does let the AI
structure, an answer that was not built from the transcript at all (for
instance the question to please supply a text) is discarded and exported
rule-based. If the
transcript carries no text at all — a recording without speech, say — the
export fails with a message instead of producing a PDF that holds nothing but
the header line.

**PDF export (all)** creates one collective PDF: each file follows as its own
section separated by spacing only — no table of contents and no extra titles.
Finished PDFs appear in the **Exports (PDF)** section for download or deletion;
in the workspace they live under `exports/`.
A running export sits in that section's heading — the current step as text,
next to it the progress in percent with a ring and the stop button, all in
front of the collapse marker. The run therefore stays visible whether the
section is open or not; like the **Files** section below it, it collapses
and expands from its heading.
A PDF made from a selection is named after its first file with the number of
further ones behind it (`one+2.pdf`), so it overwrites neither the export of
the whole transcript nor that of a single file.

Every row in that section carries a checkbox too. Several selected PDFs can be
**deleted** together or **downloaded** in one go — more than one arrives as a
zip file, so the browser only asks once.

**Verba asks before every delete.** File, PDF, segment in the editor,
transcript type, model, API key, account: the same dialog appears everywhere
and names what disappears — for a selection also how many. What is deleted
cannot be brought back.

## Search {#search}

The search no longer has a tab of its own: it sits as the **search bar** at
the top of the **Transcripts** tab and stays there while the list scrolls
under it. It searches all transcripts at once — semantically (meaning counts;
German questions also find English or Russian content) and via full text
(proper names and rare terms match exactly).

**The two buttons on the left of the field.** The **magnifier** searches and
shows the hit list. The **spark** next to it has the language model answer the
question instead — with sources that jump to the passage they quote; it only
appears where an LLM is set up. As long as something is being asked, the
result takes the place of the transcript cards. A click on the **×** on the
right of the field (or emptying the field by hand) brings them back.

**The filter tags.** Below the field is a row of tags that can be pushed
sideways: **Type**, **Date**, **Language**, **Speaker** and **State**. A click
opens a dialog in which several values are ticked at once. For the date it is a
calendar: the first click sets the start, the second the end, and the stretch
between them is shown while the pointer moves; a single click plus **OK** means
exactly that one day. Above it the quick answers for today, the last 7 or 30
days and the current year, below it the range that is actually there. For every
other filter only what actually occurs is on offer. After **OK** the
filter takes effect at once: the overview keeps only transcripts that have at
least one matching file, and their numbers count those files alone. A tag that
is set carries an **×** on its right that clears only it; **Reset all
filters** at the end of the row clears everything. The same filters apply to
the hit list and to the AI answer.

**The hit list.** Every file appears exactly once, with all of its matches
below it. The card is headed by the file's **header** — the three header
fields, the empty ones left out — and only where none of them is filled does
the file name stand in for it. At the foot of the card, quietly, is the
source: the file name; a click on it opens the editor. Each match says on the
left where it sits: the timestamp in the transcript, **Header**, **Cleaned
up** or **Translation**. A click opens exactly that place — in the transcript
the audio jumps there, for a cleaned-up or translated text the panel holding
it opens. Of the passage only the matches are shown: one to three lines
around each match, the search terms highlighted inside them, everything in
between elided with “…”. Emptying the search field clears the list along with
the question.

**The timestamp points at the search term.** A passage can span several
segments; what is shown and jumped to is the segment the search term actually
stands in — not the beginning of the passage. Only for a purely semantic hit,
where no search word appears literally, is the start of the passage the right
answer.

**Everything belonging to a recording is searched.** The transcript, the
cleaned-up text and every translation sit in the index separately. That is
more than a convenience: what Whisper misheard is corrected by the cleanup
first — a name is then only findable there. And a translation makes a
recording findable in a language never spoken in it. Every new or changed
cleanup and translation is reindexed automatically.

**The header is searched too.** A name, a date or an extra note lives in a
file's header, not in the spoken text — so Verba searches there as well: the
three header fields, the recording date and the file name. Such a hit appears
marked **Header** and quotes exactly the fields that matched, so it is visible
whether the name matched or the file name did. It is matched literally and
with all search terms at once, so “Meier 2024” finds that one file instead of
every file with a 2024 somewhere in it; the date may be written in German
notation (“12.05.2024”).

**What is not matched does not make the list.** The semantic half of the
search only ranks — left to itself it always returns the nearest passages,
even when none of them is about the question, and on a small index that is
simply the whole index. Verba therefore only lets through what is genuinely
close to the question. A question nothing answers comes back empty rather
than with arbitrary passages. The literal half (full text and header) is
unaffected: a rare name still matches exactly.

With an LLM configured, **“AI answer”** sits right next to **Search** and
produces an answer that backs every statement with numbered sources. The
source list gives number, transcript, file and position only — the passage
itself stands in the answer above — and is clickable like a hit. The AI
answers only from the retrieved passages; if there are none, it says so
honestly instead of guessing.

New transcriptions, segment edits and every saved cleanup or translation are
indexed automatically; deleted files disappear from the index immediately.
Whether a single file is in the index is what the fourth step badge on its
card (the magnifier) says.
After an update of Verba the index can be older than what it is meant to know
— the settings say so then, and the rebuild button takes the missing texts
in. **Settings → Search** shows the index
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
"already on disk" in the pick list.

A HuggingFace cache stores a snapshot's files as links into `blobs/` only.
Windows can refuse to follow such a link ("untrusted mount point", WinError
448) — in a cloud or virtual drive, in a volume mounted as a folder, or in an
encrypted container; the drive letter says nothing about it. A model in there
cannot be loaded, and a line below the field says the directory is not usable.
The remedy: a **plain folder** without links (`bge-m3/`), or the default
directory — Verba then downloads the model there once itself.

Switching models invalidates every
stored vector and therefore starts a full reindex automatically. If the status
says the index came from another model, **Rebuild index** is all it takes.

## Settings {#settings}

The settings are organised into sections: on a phone — like in a native app —
a list of sections appears first; tapping one opens it as its own page ("All
settings" leads back). On desktop the section list sits as a sidebar next to
the selected section. **Save** appears only in the sections that hold
settings, and only becomes clickable once something has changed; a short
message confirms the save.

Explanations no longer sit under the fields: a **?** follows the label.
Hover it, tap it or reach it with the keyboard to see the text; a click
elsewhere or Esc hides it again. It works the same everywhere in Verba —
the transcript types, the editor and the dialogs included. What stays
visible is only what changes: the current path, the verdict for this
machine. The state of a row sits behind the **?** next to its name as well —
whether a new version is waiting, why the GPU libraries are missing, what
the system update last did. A run in progress stays in sight regardless:
its progress bar and its log are below the row as before.

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
- **Keep the original of a cut recording:** when a recording is cut in the
  editor, Verba puts the untouched file aside once — in the transcript's
  `.original` folder, together with the transcript that belonged to it. That is
  exactly what "restore the original" in the editor brings both back from. One
  copy per edited recording, not per cut. Switched off, an applied cut is final.
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
- **Load:** above that, the system area shows what the machine is doing right
  now: one coloured bar each for the processor, the memory and — where an
  NVIDIA graphics card is present — its load and its graphics memory. The
  values are fetched every two seconds for as long as the page is open; every
  bar is filled up to its own maximum (100 % for a load, the whole memory for
  RAM and VRAM), and the figure next to it names the current value. Without a
  graphics card the two GPU bars are absent. This is how you see whether the
  machine still has room before a transcription is started — and what is
  keeping it busy during a run.

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
