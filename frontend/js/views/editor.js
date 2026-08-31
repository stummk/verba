// Transcript editor: waveform timeline (wavesurfer), text↔audio sync,
// segment editing with autosave + undo, range re-transcription, audio cutting.

import WaveSurfer from "/vendor/wavesurfer.esm.js";
import RegionsPlugin from "/vendor/wavesurfer.regions.esm.js";
import { api } from "../api.js";
import { el, formatDuration, html, toast } from "../dom.js";
import { iconButton, iconSvg, setIcon } from "../icons.js";
import { t } from "../i18n.js";
import { languageLabel, languageName, sortedLanguages } from "../languages.js";
import { on } from "../ws.js";

const AUTOSAVE_DELAY = 700;

let wavesurfer = null;
let unsubscribers = [];

export async function render(view, _status, params) {
  const fileId = Number(params[0]);
  // deep link from search results: #/editor/<fileId>/<seconds> jumps and plays
  const startAt = params[1] !== undefined ? Number(params[1]) : null;
  destroy(); // clean up a previous editor instance (the router calls it too)

  let data, textsData, settings;
  try {
    [data, textsData, settings] = await Promise.all([
      api.getSegments(fileId),
      api.getTexts(fileId).catch(() => null),
      api.getSettings().catch(() => null),
    ]);
  } catch (error) {
    view.replaceChildren(html`<div class="card">${error.message}</div>`);
    return;
  }
  const file = data.file;
  view.classList.add("wide"); // workspace uses the full width on large displays
  let derivedTexts = textsData?.texts ?? [];
  const llmEnabled = settings?.llm?.mode && settings.llm.mode !== "none";

  view.replaceChildren(html`
    <p><a href="#/project/${file.project_id}" class="muted small">${t("editor.back")}</a></p>
    <h1>${file.filename}</h1>

    <div class="card file-header-editor">
      <h2>${t("editor.pdfHeader")}</h2>
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
      <p class="hint">${t("editor.pdfHeaderHint")}</p>
    </div>

    <div class="card">
      <div id="waveform" class="waveform"></div>
      <p class="muted small" id="wave-loading">${t("editor.loading")}</p>
      <div class="editor-controls">
        <button id="play-toggle" class="icon-btn filled" disabled
                title="${t("editor.play")}" aria-label="${t("editor.play")}"></button>
        <span id="time-display" class="muted small time-display">0:00 / ${formatDuration(file.duration)}</span>
        <span class="spacer"></span>
        <span id="selection-info" class="muted small">${t("editor.noSelection")}</span>
        <button id="range-transcribe" class="icon-btn" disabled
                title="${t("editor.retranscribe")}" aria-label="${t("editor.retranscribe")}"></button>
        <button id="audio-trim" class="icon-btn" disabled
                title="${t("editor.trim")}" aria-label="${t("editor.trim")}"></button>
        <button id="audio-cut" class="icon-btn" disabled
                title="${t("editor.cut")}" aria-label="${t("editor.cut")}"></button>
        <button id="clear-selection" class="icon-btn" disabled
                title="${t("editor.clearSelection")}" aria-label="${t("editor.clearSelection")}"></button>
      </div>
      <div class="progressbar small-bar" id="range-progress" hidden><div></div></div>
      <p class="muted small job-message" id="range-message"></p>
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
        <button id="undo-button" class="icon-btn" disabled
                title="${t("editor.undo")}" aria-label="${t("editor.undo")}"></button>
      </div>
      <div class="editor-panels" id="editor-panels">
        <section class="panel" data-panel="segments">
          <div id="segment-list" class="segment-list"></div>
          <p class="muted small" id="no-segments" hidden>${t("editor.noSegments")}</p>
        </section>
        <section class="panel" data-panel="cleanup" hidden>
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

  // icon-only tool buttons (labels live in title/aria-label)
  el("play-toggle").innerHTML = iconSvg("play");
  el("undo-button").innerHTML = iconSvg("undo");
  el("range-transcribe").innerHTML = iconSvg("mic");
  el("audio-trim").innerHTML = iconSvg("crop");
  el("audio-cut").innerHTML = iconSvg("cut");
  el("clear-selection").innerHTML = iconSvg("close");

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
    url: `/api/files/${fileId}/audio`,
    height: 96,
    waveColor: styles.getPropertyValue("--md-outline-variant").trim() || "#999",
    progressColor: styles.getPropertyValue("--md-primary").trim() || "#36c",
    cursorColor: styles.getPropertyValue("--md-error").trim() || "#c00",
    plugins: [regions],
  });
  regions.enableDragSelection({ color: "rgba(80, 120, 255, 0.18)" });

  let selection = null;

  wavesurfer.on("ready", () => {
    el("wave-loading").hidden = true;
    el("play-toggle").disabled = false;
    if (startAt !== null && Number.isFinite(startAt)) {
      wavesurfer.setTime(startAt);
      wavesurfer.play();
    }
  });
  wavesurfer.on("play", () => setIcon(el("play-toggle"), "pause", t("editor.pause")));
  wavesurfer.on("pause", () => setIcon(el("play-toggle"), "play", t("editor.play")));
  wavesurfer.on("timeupdate", (time) => {
    el("time-display").textContent =
      `${formatDuration(time)} / ${formatDuration(wavesurfer.getDuration())}`;
    highlightActiveSegment(time);
  });

  regions.on("region-created", (region) => {
    for (const other of regions.getRegions()) {
      if (other !== region) other.remove();
    }
    setSelection(region.start, region.end);
  });
  regions.on("region-updated", (region) => setSelection(region.start, region.end));

  function setSelection(start, end) {
    selection = { start, end };
    el("selection-info").textContent = t("editor.selection", {
      start: formatDuration(start), end: formatDuration(end),
    });
    for (const id of ["range-transcribe", "audio-trim", "audio-cut", "clear-selection"]) {
      el(id).disabled = false;
    }
  }

  function clearSelection() {
    selection = null;
    regions.clearRegions();
    el("selection-info").textContent = t("editor.noSelection");
    for (const id of ["range-transcribe", "audio-trim", "audio-cut", "clear-selection"]) {
      el(id).disabled = true;
    }
  }

  el("play-toggle").onclick = () => wavesurfer.playPause();
  el("clear-selection").onclick = clearSelection;

  el("range-transcribe").onclick = async () => {
    if (!selection) return;
    try {
      await api.transcribeRange(fileId, selection.start, selection.end);
      toast(t("editor.rangeStarted"));
    } catch (error) {
      toast(error.message);
    }
  };
  el("audio-trim").onclick = () => runAudioEdit("trim");
  el("audio-cut").onclick = () => runAudioEdit("cut");

  async function runAudioEdit(op) {
    if (!selection) return;
    try {
      await api.editAudio(fileId, op, selection.start, selection.end);
      toast(t("editor.editStarted"));
      clearSelection();
    } catch (error) {
      toast(error.message);
    }
  }

  // ── segment list with autosave + undo ───────────────────────────────
  let segments = data.segments;
  const timers = new Map();   // segment id -> debounce timer
  const undoStack = [];       // {id, field, before}

  renderSegments();

  function renderSegments() {
    const list = el("segment-list");
    el("no-segments").hidden = segments.length > 0;
    list.replaceChildren(...segments.map((segment) => buildRow(segment)));
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
    ts.onclick = () => {
      wavesurfer.setTime(segment.start_s);
      if (!wavesurfer.isPlaying()) wavesurfer.play();
    };

    const speaker = document.createElement("input");
    speaker.className = "seg-speaker";
    speaker.value = segment.speaker ?? "";
    speaker.placeholder = t("editor.speakerPlaceholder");
    bindAutosave(speaker, segment.id, "speaker");

    const text = document.createElement("textarea");
    text.className = "seg-text";
    text.rows = 1;
    text.value = segment.text;
    bindAutosave(text, segment.id, "text");
    queueMicrotask(() => autoGrow(text));
    text.addEventListener("input", () => autoGrow(text));

    const remove = iconButton("close", t("common.delete"));
    remove.classList.add("seg-delete");
    remove.onclick = async () => {
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
  function highlightActiveSegment(time) {
    const rows = el("segment-list")?.children ?? [];
    let match = null;
    for (const row of rows) {
      if (Number(row.dataset.start) <= time && time < Number(row.dataset.end)) {
        match = row;
        break;
      }
    }
    if (match === activeRow) return;
    activeRow?.classList.remove("active");
    match?.classList.add("active");
    activeRow = match;
    if (match && wavesurfer.isPlaying()) {
      match.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  // ── workspace panels: segments | cleaned text | translations ────────
  const desktopQuery = matchMedia("(min-width: 68.75em)");
  const activePanels = new Set(["segments"]);
  let translationLanguage =
    derivedTexts.find((x) => x.kind === "translation")?.language ?? "en";
  const textTimers = new Map();

  function setupPanels() {
    if (desktopQuery.matches) {
      if (derivedTexts.some((x) => x.kind === "cleanup")) activePanels.add("cleanup");
      if (derivedTexts.some((x) => x.kind === "translation")) activePanels.add("translation");
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

  function applyPanels() {
    for (const panel of el("editor-panels").querySelectorAll(".panel")) {
      panel.hidden = !activePanels.has(panel.dataset.panel);
    }
    for (const tab of el("panel-tabs").querySelectorAll("button")) {
      tab.classList.toggle("active", activePanels.has(tab.dataset.panel));
    }
  }

  function derivedText(kind, language = "") {
    return derivedTexts.find((x) => x.kind === kind && (!language || x.language === language));
  }

  function renderDerivedPanels() {
    renderTextPanel("cleanup-panel", "cleanup", "");
    renderTranslationPanel();
  }

  function buildTextEditor(text, kind, language) {
    const area = document.createElement("textarea");
    area.className = "dtext";
    area.value = text.content;
    bindTextAutosave(area, kind, language);
    const meta = document.createElement("p");
    meta.className = "muted small";
    meta.textContent = t("editor.textMeta", {
      model: text.model || "—", date: text.created_at,
    });
    return [area, meta];
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
    if (!host) return;
    const select = document.createElement("select");
    select.className = "lang-select";
    // existing translations first, then every language as a create target
    const existing = [...new Set(
      derivedTexts.filter((x) => x.kind === "translation").map((x) => x.language)
    )];
    for (const code of existing) {
      select.append(new Option(languageLabel(code), code));
    }
    for (const { code } of sortedLanguages()) {
      if (!existing.includes(code)) select.append(new Option(languageLabel(code), code));
    }
    select.value = translationLanguage;
    const body = document.createElement("div");
    const fillBody = () => {
      const text = derivedText("translation", translationLanguage);
      body.replaceChildren(
        ...(text
          ? buildTextEditor(text, "translation", translationLanguage)
          : [buildCreateAction("translation", translationLanguage)])
      );
    };
    // switching the language only swaps the body — the select stays put
    select.onchange = () => {
      translationLanguage = select.value;
      fillBody();
    };
    fillBody();
    host.replaceChildren(select, body);
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
    button.onclick = async () => {
      const steps = kind === "cleanup" ? ["cleanup"] : ["translate"];
      try {
        await api.processFile(fileId, { steps, target_language: language });
        toast(t("ai.startedFile"));
        button.disabled = true;
      } catch (error) {
        toast(error.message);
      }
    };
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
      if (fresh) {
        segments = fresh.segments;
        renderSegments();
      }
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
      if (job.kind === "transcribe_range") {
        const progress = el("range-progress");
        const message = el("range-message");
        if (!progress) return;
        const active = job.status === "running" || job.status === "queued";
        progress.hidden = !active;
        if (active) progress.firstElementChild.style.width = `${job.progress}%`;
        message.textContent = active ? job.message : "";
      }
      if (job.kind === "llm_process") {
        const progress = el("ai-progress");
        const message = el("ai-message");
        if (!progress) return;
        const active = job.status === "running" || job.status === "queued";
        progress.hidden = !active;
        if (active) progress.firstElementChild.style.width = `${job.progress}%`;
        message.textContent = active ? (job.message || t("ai.title")) : "";
      }
    }),
  ];

  setupPanels();
}

function autoGrow(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

// Called by the router when another view takes over — and by render() for a
// second editor in a row (deep link from a search hit).
export function destroy() {
  document.getElementById("view")?.classList.remove("wide");
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  if (wavesurfer) {
    try { wavesurfer.destroy(); } catch { /* already gone */ }
    wavesurfer = null;
  }
}
