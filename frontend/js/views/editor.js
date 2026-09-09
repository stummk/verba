// Transcript editor: waveform timeline (wavesurfer), text↔audio sync,
// segment editing with autosave + undo, the language of the recording,
// transcribing the whole file or the selected passages, and cutting the
// recording itself — collected first, written in one pass (services/audio.py).

import WaveSurfer from "/vendor/wavesurfer.esm.js";
import RegionsPlugin from "/vendor/wavesurfer.regions.esm.js";
import { api } from "../api.js";
import { confirmAction, confirmDelete } from "../confirm.js";
import { el, formatDuration, html, raw, toast } from "../dom.js";
import { closeExportDialog, openExportDialog } from "../export-dialog.js";
import { iconButton, iconSvg, setIcon } from "../icons.js";
import { labelHelp } from "../help.js";
import { currentLanguage, t } from "../i18n.js";
import { languageChip, setChipLanguage } from "../language-chip.js";
import { languageName } from "../languages.js";
import * as tl from "../timeline.js";
import { on } from "../ws.js";

const AUTOSAVE_DELAY = 700;
const SELECTION_COLOR = "rgba(80, 120, 255, 0.18)";
// What a pending cut looks like: the passage is still there and still audible,
// it is only *going* to go — so it is marked, not hidden, and in the colour
// everything destructive uses.
const CUT_COLOR = "rgba(220, 60, 60, 0.28)";
const PENDING_CUTS_KEY = "verba.pendingCuts.";
// How far the stored length may be off before the marks count as stale: a
// container's duration is read back to the millisecond, a cut changes it by
// seconds.
const STALE_MARKS_TOLERANCE = 0.5;
// A cursor this close to a span's end counts as "at the end", not "inside":
// pressing play there means play it again, not play the last frame.
const RESUME_MARGIN = 0.15;
// everything that acts on the selected passage — enabled and disabled together
const SELECTION_BUTTONS = [
  "range-transcribe", "range-add-segment", "audio-trim", "audio-cut", "clear-selection",
];
const SPELLCHECK_KEY = "verba.spellcheck";

let wavesurfer = null;
let unsubscribers = [];

// The audio is fetched with the file's own URL plus a stamp: after a cut the
// recording behind that URL is a different one, and without the stamp the
// browser would happily show the waveform of the file as it used to be.
function audioUrl(fileId) {
  return `/api/files/${fileId}/audio?t=${Date.now()}`;
}

// Cuts that are collected but not yet applied survive a reload — they are a
// few numbers, and losing ten minutes of careful marking to a stray F5 is not
// something an editor should do. They stay in this browser and reach nobody
// else: nothing has happened to the recording yet.
function loadPendingCuts(fileId, duration) {
  try {
    const stored = JSON.parse(localStorage.getItem(PENDING_CUTS_KEY + fileId) || "null");
    if (!stored?.keeps?.length) {
      localStorage.removeItem(PENDING_CUTS_KEY + fileId);
      return null;
    }
    // The marks describe the recording they were drawn on. If that recording
    // has changed since — somebody else applied a cut, or put the original
    // back — they name passages that have moved, and applying them would take
    // out the wrong audio. An open editor hears about such a change and drops
    // them (reloadAudio); a closed one has to notice it here, and the length
    // is what tells it.
    if (Math.abs(Number(stored.duration) - duration) > STALE_MARKS_TOLERANCE) {
      localStorage.removeItem(PENDING_CUTS_KEY + fileId);
      return null;
    }
    return stored.keeps;
  } catch {
    return null;
  }
}

function savePendingCuts(fileId, keeps, duration) {
  try {
    if (keeps) {
      localStorage.setItem(PENDING_CUTS_KEY + fileId, JSON.stringify({ duration, keeps }));
    } else {
      localStorage.removeItem(PENDING_CUTS_KEY + fileId);
    }
  } catch { /* a browser with no storage simply forgets them */ }
}

export async function render(view, _status, params) {
  const fileId = Number(params[0]);
  // deep link from search results: #/editor/<fileId>/<seconds> jumps and plays
  const startAt = params[1] !== undefined ? Number(params[1]) : null;
  // Third path segment of the same link: which text a hit was found in —
  // "cleanup", or "translation-<code>". The cleaned-up text and the
  // translations are indexed too, and a hit in one of them belongs in the
  // panel that holds it, not at an audio position it does not have.
  const wanted = String(params[2] ?? "");
  const wantedPanel = wanted.startsWith("translation") ? "translation" : wanted;
  destroy(); // clean up a previous editor instance (the router calls it too)

  let data, textsData, settings, project;
  try {
    data = await api.getSegments(fileId);
    [textsData, settings, project] = await Promise.all([
      api.getTexts(fileId).catch(() => null),
      api.getSettings().catch(() => null),
      api.getProject(data.file.project_id).catch(() => null),
    ]);
  } catch (error) {
    view.replaceChildren(html`<div class="card">${error.message}</div>`);
    return;
  }
  const file = data.file;
  // a dialogue layout builds its PDF from the segments (it needs the speakers),
  // every other layout prefers the cleaned text — the hint below says which
  // the file's own type where it named one, its project's otherwise
  const structure = file.type_id ? file.type_structure : project?.type_structure;
  const dialogueLayout = ["dialogue", "script"].includes(structure);
  const projectFiles = project?.files ?? [];
  view.classList.add("wide"); // workspace uses the full width on large displays
  let derivedTexts = textsData?.texts ?? [];
  const llmEnabled = settings?.llm?.mode && settings.llm.mode !== "none";

  view.replaceChildren(html`
    <p><a href="#/project/${file.project_id}" class="muted small">${t("editor.back")}</a></p>
    <div class="editor-title-row">
      <h1 class="view-title-text" title="${file.filename}">${file.filename}</h1>
      <span class="spacer"></span>
      <select id="file-switch" class="file-switch" hidden
              aria-label="${t("editor.switchFile")}" title="${t("editor.switchFile")}"></select>
      <button id="editor-export" class="icon-btn"
              title="${t("export.file")}" aria-label="${t("export.file")}"></button>
    </div>

    <div class="card file-header-editor">
      ${raw(labelHelp(
        `<h2>${t("editor.pdfHeader")}</h2>`, t("editor.pdfHeaderHint"), "inline-row"
      ))}
      <div class="form-grid header-fields">
        <label>${t("editor.pdfHeaderLeft")}
          <input id="header-left" value="${file.header_left ?? ""}" maxlength="500">
        </label>
        <label>${t("editor.pdfHeaderMiddle")}
          <input id="header-middle" value="${file.header_middle ?? ""}" maxlength="500">
        </label>
        <label>${t("editor.pdfHeaderRight")}
          <input id="header-right" value="${file.header_right ?? ""}" maxlength="500">
        </label>
      </div>
    </div>

    <div class="card timeline-card">
      <div id="waveform" class="waveform"></div>
      <!-- what the waveform itself has to say — the loading state and what is
           selected on it — stands directly under it, not among the buttons -->
      <div class="wave-status">
        <p class="muted small" id="wave-loading">${t("editor.loading")}</p>
        <span class="spacer"></span>
        <span id="selection-info" class="muted small">${t("editor.noSelection")}</span>
      </div>
      <div class="editor-controls">
        <button id="play-toggle" class="icon-btn filled" disabled
                title="${t("editor.play")}" aria-label="${t("editor.play")}"></button>
        <span id="time-display" class="muted small time-display">0:00 / ${formatDuration(file.duration)}</span>
        <span class="spacer"></span>
        <span id="file-language-host"></span>
        <button id="file-transcribe" class="icon-btn"
                title="${t("editor.retranscribeFile")}"
                aria-label="${t("editor.retranscribeFile")}"></button>
        <button id="range-transcribe" class="icon-btn" disabled
                title="${t("editor.retranscribe")}" aria-label="${t("editor.retranscribe")}"></button>
        <button id="range-add-segment" class="icon-btn" disabled
                title="${t("editor.addSegment")}" aria-label="${t("editor.addSegment")}"></button>
        <button id="audio-trim" class="icon-btn" disabled
                title="${t("editor.trim")}" aria-label="${t("editor.trim")}"></button>
        <button id="audio-cut" class="icon-btn" disabled
                title="${t("editor.cut")}" aria-label="${t("editor.cut")}"></button>
        <button id="clear-selection" class="icon-btn" disabled
                title="${t("editor.clearSelection")}" aria-label="${t("editor.clearSelection")}"></button>
        <button id="audio-restore" class="icon-btn" hidden
                title="${t("editor.restoreOriginal")}"
                aria-label="${t("editor.restoreOriginal")}"></button>
      </div>
      <!-- Cuts are collected, not carried out: the recording only changes when
           the user says so, and until then this bar is the whole state of the
           editing — what will go, what will be left, and the way back. -->
      <div class="cut-pending" id="cut-pending" hidden>
        <span class="cut-badge" id="cut-summary"></span>
        <span class="spacer"></span>
        <button type="button" class="text-btn small-btn" id="cut-undo">
          ${t("editor.cutUndo")}
        </button>
        <button type="button" class="text-btn small-btn" id="cut-discard">
          ${t("editor.cutDiscard")}
        </button>
        <button type="button" class="tonal small-btn" id="cut-apply">
          ${t("editor.cutApply")}
        </button>
      </div>
      <div class="progressbar small-bar" id="range-progress" hidden><div></div></div>
      <p class="muted small job-message" id="range-message"></p>
      <div class="range-result" id="range-result" hidden>
        <div class="range-result-head">
          <span class="muted small" id="range-result-span"></span>
          <span class="spacer"></span>
          <button type="button" class="icon-btn" id="range-copy"
                  title="${t("editor.rangeCopyAll")}"
                  aria-label="${t("editor.rangeCopyAll")}"></button>
          <button type="button" class="icon-btn" id="range-result-close"
                  title="${t("common.close")}" aria-label="${t("common.close")}"></button>
        </div>
        <div id="range-result-list"></div>
      </div>
    </div>

    <div class="card workspace-card">
      <div class="editor-head">
        <div class="panel-tabs" id="panel-tabs">
          <button type="button" data-panel="segments" class="active">${t("editor.segments")}</button>
          <button type="button" data-panel="cleanup">${t("ai.tabCleanup")}</button>
          <button type="button" data-panel="translation">${t("ai.tabTranslation")}</button>
        </div>
        <span class="spacer"></span>
        <span id="save-state" class="muted small"></span>
        <!-- the language of the translation and the way to delete it belong to
             the panel, but sit here with the other tools: the panel itself is
             then nothing but the text, and on a narrow screen the chip does not
             eat a line of its own above it -->
        <span id="translation-tools" class="translation-tools" hidden></span>
        <button id="spellcheck-toggle" class="icon-btn"
                title="${t("editor.spellcheck")}" aria-label="${t("editor.spellcheck")}"></button>
        <button id="undo-button" class="icon-btn" disabled
                title="${t("editor.undo")}" aria-label="${t("editor.undo")}"></button>
      </div>
      <div class="editor-panels" id="editor-panels">
        <section class="panel" data-panel="segments">
          <p class="hint source-hint" id="segments-hint" hidden></p>
          <div class="panel-scroll" id="segment-scroll">
            <div id="segment-list" class="segment-list"></div>
          </div>
          <p class="muted small" id="no-segments" hidden>${t("editor.noSegments")}</p>
        </section>
        <section class="panel" data-panel="cleanup" hidden>
          <p class="hint source-hint" id="cleanup-hint" hidden></p>
          <div id="cleanup-panel"></div>
        </section>
        <section class="panel" data-panel="translation" hidden>
          <div id="translation-panel"></div>
        </section>
      </div>
      <div class="progressbar small-bar" id="ai-progress" hidden><div></div></div>
      <p class="muted small job-message" id="ai-message"></p>
    </div>
  `);

  // ── spell checking: the browser's own, in the language of the text ──
  //
  // Every editable text says which language it is in, so the browser reaches
  // for the right dictionary — a translation is checked against its target
  // language, not against the transcript's. It marks only what it has a
  // dictionary for; a language the browser does not know simply stays
  // unchecked. Speaker names are exempt: they are names, and every one of
  // them would be underlined.
  let sourceLanguage = file.language || settings?.whisper?.language || currentLanguage();
  let spellcheckOn = localStorage.getItem(SPELLCHECK_KEY) !== "off";

  function bindSpellcheck(field, language) {
    field.lang = language || "";
    field.dataset.spell = "1";
    field.spellcheck = spellcheckOn;
  }

  // Segments and the cleaned text are in the language of the recording — they
  // are marked as such, so correcting that language re-points their dictionary
  // right away instead of only on the next visit.
  function bindSourceSpellcheck(field) {
    bindSpellcheck(field, sourceLanguage);
    field.dataset.spellSource = "1";
  }

  function applySpellcheck() {
    for (const field of view.querySelectorAll("[data-spell]")) field.spellcheck = spellcheckOn;
    const button = el("spellcheck-toggle");
    button.setAttribute("aria-pressed", String(spellcheckOn));
    button.classList.toggle("active", spellcheckOn);
  }

  // icon-only tool buttons (labels live in title/aria-label)
  el("play-toggle").innerHTML = iconSvg("play");
  el("undo-button").innerHTML = iconSvg("undo");
  el("spellcheck-toggle").innerHTML = iconSvg("spellcheck");
  el("file-transcribe").innerHTML = iconSvg("refresh");
  el("range-transcribe").innerHTML = iconSvg("speechToText");
  el("audio-trim").innerHTML = iconSvg("crop");
  el("audio-cut").innerHTML = iconSvg("cut");
  el("clear-selection").innerHTML = iconSvg("close");
  el("range-add-segment").innerHTML = iconSvg("add");
  el("audio-restore").innerHTML = iconSvg("restore");
  el("range-copy").innerHTML = iconSvg("copy");
  el("range-result-close").innerHTML = iconSvg("close");
  el("editor-export").innerHTML = iconSvg("pdf");
  // The editor has no export list to pick the finished PDF from, so it hands
  // it straight to the browser: the export it started itself, recognised by
  // its job id, and named by the job — only the handler knows which of the
  // files in the folder it wrote.
  let exportJobId = null;
  el("editor-export").onclick = () => openExportDialog({
    fileId,
    onStarted: (job) => { exportJobId = job?.id ?? null; },
  });
  el("spellcheck-toggle").onclick = () => {
    spellcheckOn = !spellcheckOn;
    localStorage.setItem(SPELLCHECK_KEY, spellcheckOn ? "on" : "off");
    applySpellcheck();
  };
  applySpellcheck();

  // ── the language of the recording ───────────────────────────────────
  //
  // Whisper detects it from the first seconds and does get it wrong; from
  // there on the whole chain works in the wrong language — the transcription,
  // the cleanup, and a translation that is then labelled with a language it is
  // not in. So the language is stated here, and the next transcription takes
  // it as given instead of detecting again.
  const audioChipOptions = {
    autoTitle: t("editor.audioLanguageAuto"),
    hint: t("editor.audioLanguageHint"),
  };
  const audioLanguageChip = languageChip({
    ...audioChipOptions,
    code: file.language ?? "",
    dialogTitle: t("editor.audioLanguage"),
    onPick: async (chosen) => {
      try {
        const updated = await api.updateFileLanguage(fileId, chosen);
        Object.assign(file, updated);
        setChipLanguage(audioLanguageChip, file.language ?? "", audioChipOptions);
        sourceLanguage = file.language || settings?.whisper?.language || currentLanguage();
        for (const field of view.querySelectorAll("[data-spell-source]")) {
          field.lang = sourceLanguage;
        }
        toast(chosen
          ? t("editor.audioLanguageSaved", { lang: languageName(chosen) })
          : t("editor.audioLanguageCleared"));
      } catch (error) {
        // the chip keeps showing what the file still says
        toast(error.message);
      }
    },
  });
  el("file-language-host").append(audioLanguageChip);

  // ── switch between the files of the same transcript ────────────────
  // the open file is listed and selected, so the dropdown says where one is
  if (projectFiles.length > 1) {
    const switcher = el("file-switch");
    switcher.hidden = false;
    for (const other of projectFiles) {
      switcher.append(new Option(other.filename, String(other.id)));
    }
    switcher.value = String(fileId);
    switcher.onchange = () => {
      if (switcher.value && switcher.value !== String(fileId)) {
        location.hash = `#/editor/${switcher.value}`;
      }
    };
  }

  const headerTimers = new Map();
  for (const field of ["left", "middle", "right"]) {
    const input = el(`header-${field}`);
    input.addEventListener("input", () => {
      el("save-state").textContent = "…";
      clearTimeout(headerTimers.get(field));
      headerTimers.set(field, setTimeout(async () => {
        try {
          const updated = await api.updateFileHeader(fileId, {
            header_left: el("header-left").value,
            header_middle: el("header-middle").value,
            header_right: el("header-right").value,
          });
          Object.assign(file, updated);
          el("save-state").textContent = t("editor.saved");
        } catch (error) {
          el("save-state").textContent = "";
          toast(t("editor.saveError", { message: error.message }));
        }
      }, AUTOSAVE_DELAY));
    });
  }

  // ── waveform ────────────────────────────────────────────────────────
  const styles = getComputedStyle(document.documentElement);
  const regions = RegionsPlugin.create();
  wavesurfer = WaveSurfer.create({
    container: "#waveform",
    url: audioUrl(fileId),
    height: 96,
    waveColor: styles.getPropertyValue("--md-outline-variant").trim() || "#999",
    progressColor: styles.getPropertyValue("--md-primary").trim() || "#36c",
    cursorColor: styles.getPropertyValue("--md-error").trim() || "#c00",
    plugins: [regions],
  });
  regions.enableDragSelection({ color: SELECTION_COLOR });

  // ── what is selected, and what is marked for removal ────────────────
  //
  // Both are lists of spans of the recording *as it currently is*, and the
  // regions on the waveform are drawn from them — never read back as state.
  // A drag therefore does not "create a selection": it hands over a span, the
  // list decides what that means (replace, or add on Shift), and the regions
  // are rendered again from the result. Overlapping drags merge instead of
  // piling up, which is what makes several selections usable at all.
  let selections = [];      // [[start, end], …] sorted, merged
  // null = the recording is untouched; marks from an earlier visit only come
  // back when they still fit this recording (see loadPendingCuts)
  const recordingLength = () => Number(file.duration) || 0;
  let keeps = loadPendingCuts(fileId, recordingLength());
  const rememberCuts = () => savePendingCuts(fileId, keeps, recordingLength());
  const cutUndo = [];       // earlier `keeps` states, for the undo button
  let syncingRegions = false;
  let extendSelection = false;   // Shift was held when this drag began

  const waveHost = el("waveform");
  // Shift is read at the start of the drag, not at its end: the plugin only
  // reports a region once the pointer has travelled, and by then the key may
  // be up again.
  waveHost.addEventListener("pointerdown", (event) => {
    extendSelection = event.shiftKey;
  }, true);
  // The right button takes a selection away again — the counterpart to
  // Shift+drag, and the reason the browser's own menu has to stay closed.
  waveHost.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const index = tl.indexAt(selections, timeAt(event.clientX));
    if (index < 0) return;
    selections = selections.filter((_span, at) => at !== index);
    renderSelection();
  });

  function timeAt(clientX) {
    const wrapper = wavesurfer.getWrapper();
    const box = wrapper.getBoundingClientRect();
    const duration = wavesurfer.getDuration() || 0;
    return Math.min(duration, Math.max(0, ((clientX - box.left) / box.width) * duration));
  }

  function currentKeeps() {
    return keeps ?? [[0, wavesurfer.getDuration() || file.duration || 0]];
  }

  /** The passages that would be gone if the pending cuts were applied. */
  function pendingCuts() {
    if (keeps === null) return [];
    return tl.subtract([[0, wavesurfer.getDuration() || file.duration || 0]], keeps);
  }

  function renderRegions() {
    syncingRegions = true;
    regions.clearRegions();
    // the cuts first, so a selection drawn over one stays visible on top
    for (const [start, end] of pendingCuts()) {
      regions.addRegion({
        id: `cut-${start.toFixed(3)}`,
        start, end,
        color: CUT_COLOR,
        drag: false,
        resize: false,
      });
    }
    for (const [start, end] of selections) {
      regions.addRegion({ id: `sel-${start.toFixed(3)}`, start, end, color: SELECTION_COLOR });
    }
    syncingRegions = false;
  }

  function renderSelection() {
    renderRegions();
    renderSelectionInfo();
  }

  function renderSelectionInfo() {
    const info = el("selection-info");
    if (selections.length === 0) info.textContent = t("editor.noSelection");
    else if (selections.length === 1) {
      info.textContent = t("editor.selection", {
        start: formatDuration(selections[0][0]), end: formatDuration(selections[0][1]),
      });
    } else {
      info.textContent = t("editor.selectionCount", {
        count: selections.length, total: formatDuration(tl.total(selections)),
      });
    }
    for (const id of SELECTION_BUTTONS) el(id).disabled = selections.length === 0;
  }

  function clearSelections() {
    selections = [];
    renderSelection();
  }

  regions.on("region-created", (region) => {
    if (syncingRegions) return;
    const span = [region.start, region.end];
    region.remove();  // the state owns the regions, so it is drawn again below
    selections = extendSelection
      ? tl.normalize([...selections, span])
      : tl.normalize([span]);
    extendSelection = false;
    renderSelection();
  });

  // Resizing or dragging an existing selection: the state follows, but the
  // regions are deliberately *not* redrawn — that would pull the region out
  // from under the pointer mid-drag.
  regions.on("region-updated", (region) => {
    if (syncingRegions || !region.id.startsWith("sel-")) return;
    selections = tl.normalize(
      regions.getRegions()
        .filter((other) => other.id.startsWith("sel-"))
        .map((other) => [other.start, other.end])
    );
    renderSelectionInfo();
  });

  // A click on one selection plays that one alone — the quickest way to check
  // a single passage while several are marked.
  regions.on("region-clicked", (region, event) => {
    if (!region.id.startsWith("sel-")) return;
    event.stopPropagation();
    playSpans(tl.intersect([[region.start, region.end]], currentKeeps()));
  });

  // ── playback: only what is selected, and only what will be left ─────
  //
  // The selected passages are played in order and the gaps between them are
  // skipped, so the button answers "what have I selected?" — and with nothing
  // selected but cuts pending, it answers "what will this sound like?".
  let playSpansList = null;   // the spans this run walks through
  let playIndex = 0;

  function playbackSpans() {
    const base = selections.length ? selections : [[0, wavesurfer.getDuration() || 0]];
    return tl.intersect(base, currentKeeps());
  }

  function playSpans(spans) {
    playSpansList = spans;
    playIndex = 0;
    if (!spans.length) return;
    // carry on where the cursor stands if it is inside one of the spans —
    // otherwise a pause in the middle would always jump back to the start
    const time = wavesurfer.getCurrentTime();
    const at = spans.findIndex(([start, end]) => time >= start && time < end - RESUME_MARGIN);
    playIndex = Math.max(at, 0);
    if (at < 0) wavesurfer.setTime(spans[0][0]);
    wavesurfer.play();
  }

  /** Playing from one position — a segment's timestamp — ignores the spans. */
  function playFrom(time) {
    playSpansList = null;
    wavesurfer.setTime(time);
    if (!wavesurfer.isPlaying()) wavesurfer.play();
  }

  let arrived = false;   // the deep-link jump happens once, not on every reload

  wavesurfer.on("ready", () => {
    el("wave-loading").hidden = true;
    el("play-toggle").disabled = false;
    renderSelection();
    renderCutState();
    // a hit in a derived text carries no position — its link only names the
    // panel, so starting the audio from the top would be an answer to a
    // question nobody asked
    if (!arrived && startAt !== null && Number.isFinite(startAt) && !wantedPanel) {
      arrived = true;
      wavesurfer.setTime(startAt);
      wavesurfer.play();
      // a link from the search points at one hit deep in the transcript, so
      // that row is brought into view — once, on arrival
      highlightActiveSegment(startAt, { scroll: true });
    }
  });
  wavesurfer.on("play", () => setIcon(el("play-toggle"), "pause", t("editor.pause")));
  wavesurfer.on("pause", () => setIcon(el("play-toggle"), "play", t("editor.play")));
  wavesurfer.on("timeupdate", (time) => {
    el("time-display").textContent =
      `${formatDuration(time)} / ${formatDuration(wavesurfer.getDuration())}`;
    highlightActiveSegment(time);
    hopToNextSpan(time);
  });

  function hopToNextSpan(time) {
    if (!playSpansList || !wavesurfer.isPlaying()) return;
    const span = playSpansList[playIndex];
    if (!span || time < span[1]) return;
    const next = playSpansList[playIndex + 1];
    if (next) {
      playIndex += 1;
      wavesurfer.setTime(next[0]);
      return;
    }
    // The end of the last selected passage: stop there, and stay there. A
    // `timeupdate` arrives every few hundred milliseconds, so the end is
    // noticed a fraction late — the cursor is put back onto it, both to leave
    // it exactly where the selection ends and so that pressing play again
    // plays the selection rather than its last frame.
    wavesurfer.pause();
    wavesurfer.setTime(span[1]);
    playSpansList = null;
  }

  el("play-toggle").onclick = () => {
    if (wavesurfer.isPlaying()) wavesurfer.pause();
    else playSpans(playbackSpans());
  };
  el("clear-selection").onclick = clearSelections;

  // Two transcriptions that must not be confused with one another:
  //
  //   the whole file — throws the segments away and recognises them again,
  //                    which is the way out of a wrong language or model;
  //   the selection  — hands back the text of those passages and nothing else.
  //                    It writes no segments: one listens to a passage to
  //                    check what was recognised there, and a result written
  //                    back would overwrite exactly the edit one was checking.
  el("file-transcribe").onclick = async () => {
    if (segments.length) {
      const ok = await confirmAction({
        title: t("editor.retranscribeFileTitle"),
        message: t("editor.retranscribeFileConfirm"),
        confirmLabel: t("editor.retranscribeFileStart"),
      });
      if (!ok) return;
    }
    try {
      await api.transcribeFile(fileId);
      toast(t("editor.fileTranscribeStarted"));
    } catch (error) {
      toast(error.message);
    }
  };

  el("range-transcribe").onclick = async () => {
    if (!selections.length) return;
    try {
      rangeResults = [];
      renderRangeResults();
      await api.transcribeRanges(fileId, selections);
      toast(t("editor.rangeStarted"));
    } catch (error) {
      toast(error.message);
    }
  };
  // Passages the recognition left out: every selection becomes a row one can
  // type into. They carry no text — that is the point — so they are added
  // straight away and the cursor lands in the first one's field.
  el("range-add-segment").onclick = async () => {
    if (!selections.length) return;
    await addSegments(selections.map(([start, end]) => ({ start, end, text: "" })));
  };

  // ── cuts, collected until the user says so ──────────────────────────
  //
  // "Auf Auswahl kürzen" and "Auswahl entfernen" no longer touch the file and
  // no longer produce a second one: they change the list of spans that are to
  // be *kept*, the waveform strikes out what would go, and one "Übernehmen"
  // writes the whole collection into the recording in a single pass.
  el("audio-trim").onclick = () => markKeeps(tl.intersect(currentKeeps(), selections));
  el("audio-cut").onclick = () => markKeeps(tl.subtract(currentKeeps(), selections));

  // Both markings pass through here, because both can end up with nothing
  // left: removing everything that is still there, or keeping only a passage
  // that is already marked for removal. An empty result is not a cut anybody
  // can apply — the backend refuses it — so it is refused right here.
  function markKeeps(next) {
    if (!selections.length) return;
    if (!next.length) {
      toast(t("editor.cutEverything"));
      return;
    }
    setKeeps(next);
  }

  function setKeeps(next) {
    cutUndo.push(keeps);
    keeps = tl.coversAll(next, wavesurfer.getDuration() || file.duration || 0) ? null : next;
    rememberCuts();
    clearSelections();   // the selected passage is now marked, not selected
    renderCutState();
  }

  function renderCutState() {
    const bar = el("cut-pending");
    if (!bar) return;
    const cuts = pendingCuts();
    bar.hidden = cuts.length === 0;
    el("cut-undo").disabled = cutUndo.length === 0;
    if (!cuts.length) return;
    el("cut-summary").textContent = t(
      cuts.length === 1 ? "editor.cutPendingOne" : "editor.cutsPending",
      {
        count: cuts.length,
        removed: formatDuration(tl.total(cuts)),
        left: formatDuration(tl.total(currentKeeps())),
      }
    );
  }

  el("cut-undo").onclick = () => {
    if (!cutUndo.length) return;
    keeps = cutUndo.pop();
    rememberCuts();
    renderSelection();
    renderCutState();
  };
  el("cut-discard").onclick = () => {
    cutUndo.length = 0;
    keeps = null;
    rememberCuts();
    renderSelection();
    renderCutState();
  };
  el("cut-apply").onclick = async () => {
    const cuts = pendingCuts();
    if (!cuts.length) return;
    const ok = await confirmAction({
      title: t("editor.cutApplyTitle"),
      message: t(
        settings?.general?.audio_backup === false
          ? "editor.cutApplyConfirmNoBackup"
          : "editor.cutApplyConfirm",
        { removed: formatDuration(tl.total(cuts)) }
      ),
      confirmLabel: t("editor.cutApply"),
    });
    if (!ok) return;
    try {
      await api.applyAudioCuts(fileId, currentKeeps());
      toast(t("editor.cutStarted"));
    } catch (error) {
      toast(error.message);
    }
  };

  // The way back out of every cut ever applied — offered only while the
  // untouched recording is actually still lying next to it.
  async function refreshRestoreButton() {
    const button = el("audio-restore");
    if (!button) return;
    const state = await api.audioOriginalState(fileId).catch(() => null);
    button.hidden = !state?.can_restore;
  }
  refreshRestoreButton();

  el("audio-restore").onclick = async () => {
    const ok = await confirmAction({
      title: t("editor.restoreOriginalTitle"),
      message: t("editor.restoreOriginalConfirm"),
      confirmLabel: t("editor.restoreOriginal"),
    });
    if (!ok) return;
    try {
      await api.restoreAudioOriginal(fileId);
      toast(t("editor.restoreStarted"));
    } catch (error) {
      toast(error.message);
    }
  };

  /** After a cut or a restore: the audio on disk is a different file now. */
  async function reloadAudio() {
    cutUndo.length = 0;
    keeps = null;
    rememberCuts();
    selections = [];
    playSpansList = null;
    el("wave-loading").hidden = false;
    try {
      await wavesurfer.load(audioUrl(fileId));
    } catch { /* a load cancelled by the next one is not an error */ }
    renderCutState();
    refreshRestoreButton();
  }

  // ── the text of the transcribed selections ──────────────────────────
  //
  // Every passage lands in this list as its recognition finishes. It goes
  // nowhere else: the text is there to be read against what stands in the
  // segments, and each row offers the two things worth doing with it — take it
  // over as a segment, or copy it. A single passage also goes to the clipboard
  // by itself, because that is what one asks for it for.
  let rangeResults = [];

  async function copyText(text, { silent = false } = {}) {
    if (!text) return false;
    try {
      await navigator.clipboard.writeText(text);
      if (!silent) toast(t("editor.rangeCopied"));
      return true;
    } catch {
      if (!silent) toast(t("editor.rangeCopyFailed"));
      return false;
    }
  }

  function showRangeText(payload) {
    if ((payload.index ?? 0) === 0) rangeResults = [];   // a new run starts over
    rangeResults[payload.index ?? 0] = {
      start: payload.start_s, end: payload.end_s, text: payload.text ?? "",
    };
    renderRangeResults();
    if ((payload.total ?? 1) === 1 && payload.text) {
      copyText(payload.text, { silent: true }).then((copied) => {
        if (copied) toast(t("editor.rangeCopied"));
      });
    }
  }

  function renderRangeResults() {
    const host = el("range-result-list");
    const box = el("range-result");
    if (!host || !box) return;
    const found = rangeResults.filter(Boolean);
    box.hidden = found.length === 0;
    el("range-copy").disabled = !found.some((row) => row.text);
    el("range-result-span").textContent = found.length > 1
      ? t("editor.rangeResults", { count: found.length })
      : found.length === 1
        ? t("editor.rangeResult", {
            start: formatDuration(found[0].start), end: formatDuration(found[0].end),
          })
        : "";
    host.replaceChildren(...found.map((row, index) => buildRangeRow(row, index, found.length)));
  }

  function buildRangeRow(row, index, count) {
    const wrap = document.createElement("div");
    wrap.className = "range-row";

    const head = document.createElement("div");
    head.className = "range-row-head";
    const span = document.createElement("button");
    span.type = "button";
    span.className = "seg-ts";
    span.textContent = count > 1
      ? `${index + 1}. ${formatDuration(row.start)}`
      : formatDuration(row.start);
    span.title = t("editor.rangePlay");
    span.onclick = () => playSpans([[row.start, row.end]]);
    head.append(span, Object.assign(document.createElement("span"), { className: "spacer" }));

    const asSegment = iconButton("add", t("editor.rangeAsSegment"), async () => {
      await addSegments([{ start: row.start, end: row.end, text: row.text }]);
    });
    const copy = iconButton("copy", t("editor.rangeCopy"), () => copyText(row.text));
    asSegment.disabled = !row.text;
    copy.disabled = !row.text;
    head.append(asSegment, copy);

    const text = document.createElement("p");
    text.className = "range-result-text";
    text.textContent = row.text || t("editor.rangeEmpty");
    text.lang = sourceLanguage;

    wrap.append(head, text);
    return wrap;
  }

  el("range-copy").onclick = () => {
    // several passages copy as one text, in their order, one per line
    const text = rangeResults.filter((row) => row?.text).map((row) => row.text).join("\n\n");
    copyText(text);
  };
  el("range-result-close").onclick = () => {
    rangeResults = [];
    renderRangeResults();
  };

  // ── segment list with autosave + undo ───────────────────────────────
  let segments = data.segments;
  const timers = new Map();   // segment id -> debounce timer
  const undoStack = [];       // {id, field, before}

  // A row that has just been added is the one the user wants to type in — it
  // is remembered by id rather than focused on the spot, because the same
  // change also arrives as an event and renders the list a second time.
  let focusSegmentId = null;

  renderSegments();

  function renderSegments() {
    const list = el("segment-list");
    el("no-segments").hidden = segments.length > 0;
    list.replaceChildren(...segments.map((segment) => buildRow(segment)));
    if (focusSegmentId === null) return;
    const row = list.querySelector(`.seg-row[data-id="${focusSegmentId}"]`);
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    row.querySelector(".seg-text")?.focus();
  }

  // The one way segments come into being from the editor: for the selected
  // passages, empty or with the text a re-transcription handed back. Several
  // at once, because several passages can be selected — the cursor then lands
  // in the first of them, which is the one the user was looking at.
  async function addSegments(spans) {
    if (!spans.length) return;
    let created = 0;
    try {
      for (const span of spans) {
        const row = await api.createSegment(fileId, {
          start_s: span.start, end_s: span.end, text: span.text ?? "",
        });
        if (!created) focusSegmentId = row.id;
        created += 1;
      }
      showSegmentsPanel();
      const fresh = await api.getSegments(fileId);
      segments = fresh.segments;
      renderSegments();
      toast(created > 1
        ? t("editor.segmentsAdded", { count: created })
        : t("editor.segmentAdded"));
    } catch (error) {
      toast(error.message);
    } finally {
      // only now: the event about this very change may have rendered the list
      // in between, and whichever render comes last has to find the row again
      focusSegmentId = null;
    }
  }

  function buildRow(segment) {
    const row = document.createElement("div");
    row.className = "seg-row";
    row.dataset.id = segment.id;
    row.dataset.start = segment.start_s;
    row.dataset.end = segment.end_s;

    const ts = document.createElement("button");
    ts.type = "button";
    ts.className = "seg-ts";
    ts.textContent = formatDuration(segment.start_s);
    ts.onclick = () => playFrom(segment.start_s);

    const speaker = document.createElement("input");
    speaker.className = "seg-speaker";
    speaker.value = segment.speaker ?? "";
    speaker.placeholder = t("editor.speakerPlaceholder");
    speaker.spellcheck = false;
    bindAutosave(speaker, segment.id, "speaker");

    const text = document.createElement("textarea");
    text.className = "seg-text";
    text.rows = 1;
    text.value = segment.text;
    bindSourceSpellcheck(text);
    bindAutosave(text, segment.id, "text");
    queueMicrotask(() => autoGrow(text));
    text.addEventListener("input", () => autoGrow(text));

    const remove = iconButton("close", t("common.delete"));
    remove.classList.add("seg-delete");
    remove.onclick = async () => {
      // undo only reaches text and speaker edits — a deleted segment is gone
      const ok = await confirmDelete({ message: t("editor.segmentDeleteConfirm") });
      if (!ok) return;
      try {
        await api.deleteSegment(segment.id);
        segments = segments.filter((s) => s.id !== segment.id);
        renderSegments();
        toast(t("editor.segmentDeleted"));
      } catch (error) {
        toast(error.message);
      }
    };

    row.append(ts, speaker, text, remove);
    return row;
  }

  function bindAutosave(input, segmentId, field) {
    input.addEventListener("focus", () => { input.dataset.before = input.value; });
    input.addEventListener("input", () => {
      el("save-state").textContent = "…";
      clearTimeout(timers.get(`${segmentId}:${field}`));
      timers.set(`${segmentId}:${field}`, setTimeout(async () => {
        try {
          const before = input.dataset.before ?? "";
          if (before !== input.value) {
            undoStack.push({ id: segmentId, field, before });
            input.dataset.before = input.value;
            el("undo-button").disabled = false;
          }
          const updated = await api.updateSegment(segmentId, { [field]: input.value });
          const local = segments.find((s) => s.id === segmentId);
          if (local) Object.assign(local, updated);
          el("save-state").textContent = t("editor.saved");
        } catch (error) {
          el("save-state").textContent = "";
          toast(t("editor.saveError", { message: error.message }));
        }
      }, AUTOSAVE_DELAY));
    });
  }

  el("undo-button").onclick = async () => {
    const entry = undoStack.pop();
    if (!entry) return;
    el("undo-button").disabled = undoStack.length === 0;
    try {
      const updated = await api.updateSegment(entry.id, { [entry.field]: entry.before });
      const local = segments.find((s) => s.id === entry.id);
      if (local) Object.assign(local, updated);
      const row = el("segment-list").querySelector(`.seg-row[data-id="${entry.id}"]`);
      const input = row?.querySelector(entry.field === "text" ? ".seg-text" : ".seg-speaker");
      if (input) {
        input.value = entry.before;
        input.dataset.before = entry.before;
        if (entry.field === "text") autoGrow(input);
      }
      el("save-state").textContent = t("editor.saved");
    } catch (error) {
      toast(t("editor.saveError", { message: error.message }));
    }
  };

  let activeRow = null;

  // Playback marks the current segment and nothing more: a list that follows
  // along on its own pulls the row one is reading or editing out from under
  // the cursor. Only the caller that jumps somewhere asks for the scroll.
  function highlightActiveSegment(time, { scroll = false } = {}) {
    const rows = el("segment-list")?.children ?? [];
    let match = null;
    for (const row of rows) {
      if (Number(row.dataset.start) <= time && time < Number(row.dataset.end)) {
        match = row;
        break;
      }
    }
    if (match !== activeRow) {
      activeRow?.classList.remove("active");
      match?.classList.add("active");
      activeRow = match;
    }
    // not tied to the mark changing: seeking to the hit may have marked it
    // already, and the row would then never be scrolled to
    if (match && scroll) match.scrollIntoView({ block: "center" });
  }

  // ── workspace panels: segments | cleaned text | translations ────────
  const desktopQuery = matchMedia("(min-width: 68.75em)");
  const activePanels = new Set(["segments"]);
  let translationLanguage =
    wanted.startsWith("translation-") && wanted.slice("translation-".length)
      ? wanted.slice("translation-".length)
      : (derivedTexts.find((x) => x.kind === "translation" && x.content.trim())?.language ?? "en");
  const textTimers = new Map();

  function setupPanels() {
    if (desktopQuery.matches) {
      if (hasText("cleanup")) activePanels.add("cleanup");
      if (hasText("translation")) activePanels.add("translation");
    }
    if (wantedPanel === "cleanup" || wantedPanel === "translation") {
      if (!desktopQuery.matches) activePanels.clear();
      activePanels.add(wantedPanel);
    }
    for (const tab of el("panel-tabs").querySelectorAll("button")) {
      tab.onclick = () => {
        const name = tab.dataset.panel;
        if (desktopQuery.matches) {
          if (activePanels.has(name) && activePanels.size > 1) activePanels.delete(name);
          else activePanels.add(name);
        } else {
          activePanels.clear();
          activePanels.add(name);
        }
        applyPanels();
      };
    }
    desktopQuery.addEventListener("change", () => {
      if (!desktopQuery.matches && activePanels.size > 1) {
        const first = activePanels.values().next().value;
        activePanels.clear();
        activePanels.add(first);
      }
      applyPanels();
    });
    applyPanels();
    renderDerivedPanels();
  }

  // On a narrow screen only one panel is open at a time — a new segment row
  // is of no use behind the cleaned text.
  function showSegmentsPanel() {
    if (activePanels.has("segments")) return;
    if (!desktopQuery.matches) activePanels.clear();
    activePanels.add("segments");
    applyPanels();
  }

  function applyPanels() {
    for (const panel of el("editor-panels").querySelectorAll(".panel")) {
      panel.hidden = !activePanels.has(panel.dataset.panel);
    }
    for (const tab of el("panel-tabs").querySelectorAll("button")) {
      tab.classList.toggle("active", activePanels.has(tab.dataset.panel));
    }
    el("translation-tools").hidden = !activePanels.has("translation");
    bindScrollSync();
  }

  // Segments and derived text scroll together — side by side they are only
  // comparable when both move. The cleaned text is one flowing text, not one
  // block per segment, so the mapping is proportional, not line-exact.
  let scrollLock = false;

  function scrollBoxes() {
    const panels = el("editor-panels");
    if (!panels) return [];
    return [...panels.querySelectorAll(".panel:not([hidden]) .panel-scroll, "
      + ".panel:not([hidden]) .dtext")];
  }

  function bindScrollSync() {
    for (const box of scrollBoxes()) {
      if (box.dataset.scrollSync) continue; // panels re-render, listeners do not
      box.dataset.scrollSync = "1";
      box.addEventListener("scroll", () => {
        if (scrollLock) return;
        const max = box.scrollHeight - box.clientHeight;
        if (max <= 0) return;
        const others = scrollBoxes().filter((other) => other !== box);
        if (!others.length) return;
        scrollLock = true; // the mirrored scroll must not bounce back
        for (const other of others) {
          const otherMax = other.scrollHeight - other.clientHeight;
          if (otherMax > 0) other.scrollTop = (box.scrollTop / max) * otherMax;
        }
        requestAnimationFrame(() => { scrollLock = false; });
      });
    }
  }

  function derivedText(kind, language = "") {
    return derivedTexts.find((x) => x.kind === kind && (!language || x.language === language));
  }

  // an emptied text is not a result any more, only a leftover row
  function hasText(kind) {
    return derivedTexts.some((x) => x.kind === kind && x.content.trim());
  }

  function renderDerivedPanels() {
    renderTextPanel("cleanup-panel", "cleanup", "");
    renderTranslationPanel();
    renderSourceHints();
    bindScrollSync();
  }

  // Which text counts in the end — the most confusing thing about the editor:
  // as soon as a cleanup exists, it is what the translation and the PDF are
  // built from, so edits in the segments no longer reach them. A dialogue
  // layout is the exception; its PDF needs the speakers from the segments.
  function renderSourceHints() {
    const segHint = el("segments-hint");
    const cleanHint = el("cleanup-hint");
    if (!segHint || !cleanHint) return;
    const hasCleanup = Boolean(derivedText("cleanup")?.content?.trim());
    segHint.textContent = !hasCleanup
      ? t("editor.sourceSegments")
      : dialogueLayout ? t("editor.sourceDialogue") : t("editor.sourceCleanup");
    segHint.hidden = false;
    cleanHint.textContent = dialogueLayout
      ? t("editor.cleanupRoleDialogue")
      : t("editor.cleanupRole");
    cleanHint.hidden = false;
  }

  function buildTextEditor(text, kind, language) {
    const head = document.createElement("div");
    head.className = "dtext-head";
    const meta = document.createElement("span");
    meta.className = "muted small";
    meta.textContent = t("editor.textMeta", {
      model: text.model || "—", date: text.created_at,
    });
    head.append(meta, Object.assign(document.createElement("span"), { className: "spacer" }));
    if (llmEnabled) {
      // without this the step can only ever run once per file: the panel shows
      // the text and there is no way back to the model
      const rerun = document.createElement("button");
      rerun.type = "button";
      rerun.className = "text-btn small-btn";
      rerun.textContent = t("editor.rerun");
      rerun.title = t("editor.rerunHint");
      rerun.onclick = () => startStep(kind, language, rerun);
      head.append(rerun);
    }
    const area = document.createElement("textarea");
    area.className = "dtext";
    area.value = text.content;
    if (kind === "translation") bindSpellcheck(area, language);
    else bindSourceSpellcheck(area);
    bindTextAutosave(area, kind, language);
    return [head, area];
  }

  async function startStep(kind, language, button) {
    button.disabled = true;
    try {
      await api.processFile(fileId, {
        steps: kind === "cleanup" ? ["cleanup"] : ["translate"],
        target_language: language,
      });
      toast(t("ai.startedFile"));
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  }

  function renderTextPanel(hostId, kind, language) {
    const host = el(hostId);
    if (!host) return;
    const text = derivedText(kind, language);
    host.replaceChildren(
      ...(text ? buildTextEditor(text, kind, language) : [buildCreateAction(kind, language)])
    );
  }

  function renderTranslationPanel() {
    const host = el("translation-panel");
    const tools = el("translation-tools");
    if (!host || !tools) return;
    // The chip says which translation is open — flag, code, and the language
    // in its tooltip. Its picker splits the list in two, so a glance there
    // still answers "which language is already translated?".
    const existing = [...new Set(
      derivedTexts
        .filter((x) => x.kind === "translation" && x.content.trim())
        .map((x) => x.language)
    )];
    const languageOptions = {
      dialogTitle: t("ai.tabTranslation"),
      auto: false,
      groups: [{ label: t("editor.langTranslated"), codes: existing }],
      restLabel: t("editor.langOpen"),
    };
    const chip = languageChip({
      ...languageOptions,
      code: translationLanguage,
      onPick: (code) => {
        translationLanguage = code;
        setChipLanguage(chip, code, languageOptions);
        fillBody();
      },
    });
    // A translation is the one derived text that piles up: one per language,
    // and a wrong or unwanted one would otherwise stay in the file and in the
    // export list forever — clearing the textarea leaves an empty shell behind.
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn";
    remove.innerHTML = iconSvg("delete");
    remove.title = t("editor.deleteTranslation");
    remove.setAttribute("aria-label", remove.title);
    remove.onclick = async () => {
      const language = translationLanguage;
      const ok = await confirmDelete({
        message: t("editor.deleteTranslationConfirm", { lang: languageName(language) }),
      });
      if (!ok) return;
      try {
        await api.deleteText(fileId, "translation", language);
        derivedTexts = derivedTexts.filter(
          (x) => !(x.kind === "translation" && x.language === language)
        );
        renderDerivedPanels();
      } catch (error) {
        toast(error.message);
      }
    };
    const body = document.createElement("div");
    function fillBody() {
      const text = derivedText("translation", translationLanguage);
      remove.hidden = !text;
      body.replaceChildren(
        ...(text
          ? buildTextEditor(text, "translation", translationLanguage)
          : [buildCreateAction("translation", translationLanguage)])
      );
    }
    fillBody();
    tools.replaceChildren(chip, remove);
    host.replaceChildren(body);
  }

  function buildCreateAction(kind, language) {
    const wrap = document.createElement("div");
    wrap.className = "panel-empty";
    if (!llmEnabled) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent = t("editor.noLlm");
      const link = document.createElement("a");
      link.className = "text-btn small-btn";
      link.href = "#/settings";
      link.textContent = t("settings.title");
      wrap.append(hint, link);
      return wrap;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tonal";
    button.textContent = kind === "cleanup"
      ? t("editor.createCleanup")
      : t("editor.createTranslation", { lang: languageName(language) });
    button.onclick = () => startStep(kind, language, button);
    wrap.append(button);
    return wrap;
  }

  function bindTextAutosave(area, kind, language) {
    area.addEventListener("input", () => {
      el("save-state").textContent = "…";
      const key = `${kind}:${language}`;
      clearTimeout(textTimers.get(key));
      textTimers.set(key, setTimeout(async () => {
        try {
          const updated = await api.updateText(fileId, kind, language, area.value);
          const local = derivedText(kind, language);
          if (local) local.content = updated.content;
          el("save-state").textContent = t("editor.saved");
        } catch (error) {
          el("save-state").textContent = "";
          toast(t("editor.saveError", { message: error.message }));
        }
      }, AUTOSAVE_DELAY));
    });
  }

  // ── live updates (range re-transcription, AI pipeline) ──────────────
  unsubscribers = [
    on("segments.changed", async ({ file_id }) => {
      if (file_id !== fileId) return;
      const fresh = await api.getSegments(fileId).catch(() => null);
      if (!fresh) return;
      segments = fresh.segments;
      // don't re-render under the user's cursor — their own edit caused this
      const editing = document.activeElement?.closest?.(".seg-row");
      if (!editing) renderSegments();
    }),
    on("range.text", (payload) => {
      if (payload.file_id === fileId) showRangeText(payload);
    }),
    on("file.update", (row) => {
      // the language may have been corrected elsewhere (project view, a new
      // run), and the duration changes when a cut is applied
      if (row.id !== fileId) return;
      Object.assign(file, row);
      setChipLanguage(audioLanguageChip, file.language ?? "", audioChipOptions);
    }),
    on("texts.changed", async ({ file_id }) => {
      if (file_id !== fileId) return;
      const fresh = await api.getTexts(fileId).catch(() => null);
      if (!fresh) return;
      derivedTexts = fresh.texts;
      // don't re-render under the user's cursor — their edit triggered this event
      const editing = document.activeElement?.classList?.contains("dtext");
      if (!editing) renderDerivedPanels();
    }),
    on("job.update", (job) => {
      if (job.file_id !== fileId) return;
      // everything that works on the audio reports in the same line under
      // the waveform: the two transcriptions, the cut, and the restore
      if (["transcribe_range", "transcribe", "audio_edit", "audio_restore"].includes(job.kind)) {
        showJobState("range-progress", "range-message", job);
      }
      // The recording behind the waveform is a different file now — the
      // pending cuts are written (or gone), and everything drawn from the old
      // one, marks and selection included, has to go with it.
      if (job.status === "done" && (job.kind === "audio_edit" || job.kind === "audio_restore")) {
        reloadAudio();
        toast(t(job.kind === "audio_edit" ? "editor.cutDone" : "editor.restoreDone"));
      }
      if (job.kind === "llm_process") {
        showJobState("ai-progress", "ai-message", job, t("ai.title"));
        // the run is over: the panels get their start buttons back, enabled
        if (job.status === "failed" || job.status === "cancelled") renderDerivedPanels();
      }
      if (job.kind === "export_pdf" && job.id === exportJobId) {
        if (job.status === "done" && job.result) {
          exportJobId = null;
          toast(t("export.done"));
          downloadExport(file.project_id, job.result);
        } else if (job.status === "failed" || job.status === "cancelled") {
          exportJobId = null;
          // nothing on screen holds an export's state here — a failure has to
          // say so itself, or the download the user is waiting for just never
          // arrives
          if (job.error) toast(job.error);
        }
      }
    }),
  ];

  setupPanels();
}

// The finished PDF, straight into the browser's downloads. A named download
// with `rel="noopener"`, like the transcript view does it, so the file arrives
// without a tab of its own opening for it.
function downloadExport(projectId, name) {
  const link = document.createElement("a");
  link.href = api.exportUrl(projectId, name);
  link.download = name;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

// A finished job clears the line; a failed one leaves its reason standing.
// Without this a broken LLM run looks exactly like nothing having happened —
// which is what "I press the button and nothing shows up" came down to.
function showJobState(progressId, messageId, job, fallback = "") {
  const progress = el(progressId);
  const message = el(messageId);
  if (!progress || !message) return;
  const active = job.status === "running" || job.status === "queued";
  progress.hidden = !active;
  if (active) progress.firstElementChild.style.width = `${job.progress}%`;
  message.classList.toggle("error-text", job.status === "failed");
  if (active) message.textContent = job.message || fallback;
  else if (job.status === "failed") message.textContent = job.error || t("editor.jobFailed");
  else message.textContent = "";
  if (job.status === "failed" && job.error) toast(job.error);
}

function autoGrow(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

// Called by the router when another view takes over — and by render() for a
// second editor in a row (deep link from a search hit).
export function destroy() {
  document.getElementById("view")?.classList.remove("wide");
  closeExportDialog();
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  if (wavesurfer) {
    try { wavesurfer.destroy(); } catch { /* already gone */ }
    wavesurfer = null;
  }
}
