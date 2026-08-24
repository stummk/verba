// Project view: import (upload, server browser, drag & drop), file table with
// live progress, per-run advanced options (model/language), segment preview.

import { api } from "../api.js";
import { el, esc, formatDuration, html, raw, toast } from "../dom.js";
import { iconButton, iconSvg } from "../icons.js";
import { t } from "../i18n.js";
import { fillLanguageSelect } from "../languages.js";
import { on } from "../ws.js";

const AUDIO_RE = /\.(mp3|wav|m4a|flac|ogg|opus|aac|wma|webm|mp4)$/i;

let unsubscribers = [];
let fabHandler = null;
const fileJobs = new Map(); // file_id -> latest active job

export async function render(view, _status, params) {
  const projectId = Number(params[0]);
  let projectError = null;
  const [project, models, settings] = await Promise.all([
    api.getProject(projectId).catch((error) => {
      projectError = error;
      return null;
    }),
    api.listModels().catch(() => ({ builtin: [], local: [] })),
    api.getSettings().catch(() => null),
  ]);
  if (!project) {
    view.replaceChildren(html`<div class="card">${projectError.message}</div>`);
    return;
  }
  const llmEnabled = settings?.llm?.mode && settings.llm.mode !== "none";

  view.replaceChildren(html`
    <p><a href="#/" class="muted small">${t("project.back")}</a></p>
    <h1>${project.name}${project.type_name ? raw(` <span class="type-chip">${esc(project.type_name)}</span>`) : ""}</h1>
    <div class="card">
      <div class="step-tabs" role="tablist">
        <button type="button" class="step-tab" role="tab" data-step="1" aria-controls="step-panel-1">
          <span class="step-number" aria-hidden="true">1</span> ${t("project.stepImport")}
        </button>
        <button type="button" class="step-tab" role="tab" data-step="2" aria-controls="step-panel-2">
          <span class="step-number" aria-hidden="true">2</span> ${t("project.stepTranscribe")}
        </button>
        <button type="button" class="step-tab" role="tab" data-step="3" aria-controls="step-panel-3">
          <span class="step-number" aria-hidden="true">3</span> ${t("project.stepExport")}
        </button>
      </div>
      <div class="step-panel" id="step-panel-1" role="tabpanel">
        <div class="step-actions">
          <label class="btn tonal icon-label" for="file-upload">
            ${raw(iconSvg("upload"))} ${t("project.upload")}
          </label>
          <input type="file" id="file-upload" multiple hidden
                 accept=".mp3,.wav,.m4a,.flac,.ogg,.opus,.aac,.wma,.webm,.mp4">
          <button id="server-import" class="text-btn icon-label">
            ${raw(iconSvg("folder"))} ${t("project.serverImport")}
          </button>
        </div>
        <p class="hint">${t("project.workspaceHint", { path: project.workspace })}</p>
      </div>
      <div class="step-panel" id="step-panel-2" role="tabpanel" hidden>
        <div class="step-actions">
          <button id="transcribe-all" class="icon-label">
            ${raw(iconSvg("mic"))} ${t("project.transcribeAll")}
          </button>
          <button id="process-all" class="tonal icon-label" ${llmEnabled ? "" : "hidden"}>
            ${raw(iconSvg("sparkle"))} ${t("project.processAll")}
          </button>
        </div>
        <details class="advanced" id="advanced-panel">
          <summary>${t("project.advanced")}</summary>
          <div class="form-grid">
            <div>
              <label for="adv-model">${t("project.advModel")}</label>
              <select id="adv-model"></select>
            </div>
            <div>
              <label for="adv-language">${t("project.advLanguage")}</label>
              <select id="adv-language"></select>
            </div>
          </div>
        </details>
        <div class="checkline auto-line" ${llmEnabled ? "" : "hidden"}>
          <label class="checkline">
            <input type="checkbox" id="auto-process"> ${t("project.autoProcess")}
          </label>
          <select id="auto-language"></select>
        </div>
      </div>
      <div class="step-panel" id="step-panel-3" role="tabpanel" hidden>
        <div class="step-actions">
          <button id="export-all" class="tonal icon-label">
            ${raw(iconSvg("pdf"))} ${t("export.all")}
          </button>
        </div>
      </div>
    </div>
    <div class="card">
      <table class="filetable">
        <thead><tr>
          <th>${t("project.colFile")}</th><th>${t("project.colLanguage")}</th>
          <th>${t("project.colDuration")}</th>
          <th>${t("project.colStatus")}</th><th class="col-actions"></th>
        </tr></thead>
        <tbody id="file-rows"></tbody>
      </table>
      <p class="muted small" id="no-files" hidden>${t("project.noFiles")}</p>
    </div>
    <div class="card" id="exports-card" hidden>
      <h2>${t("export.exports")}</h2>
      <div id="export-list"></div>
    </div>
    <div id="browser-modal"></div>
  `);

  fillAdvancedSelects(models);

  // step tabs: one workflow step visible at a time; panels stay in the DOM
  // so their controls keep state and every handler binds once below
  function selectStep(step) {
    for (const tab of view.querySelectorAll(".step-tab")) {
      const active = tab.dataset.step === String(step);
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      if (active) tab.scrollIntoView({ inline: "nearest", block: "nearest" });
    }
    for (const panel of view.querySelectorAll(".step-panel")) {
      panel.hidden = panel.id !== `step-panel-${step}`;
    }
  }
  for (const tab of view.querySelectorAll(".step-tab")) {
    tab.onclick = () => selectStep(tab.dataset.step);
  }
  selectStep(project.files.length ? 2 : 1); // with files present, work continues at step 2

  const files = new Map(project.files.map((f) => [f.id, f]));
  const fileLanguages = new Map();
  fileJobs.clear();
  renderRows(files);

  function flowOptions() {
    return {
      model: el("adv-model")?.value ?? "",
      language: el("adv-language")?.value ?? "",
    };
  }

  async function uploadFiles(fileList) {
    const audioFiles = [...fileList].filter((f) => AUDIO_RE.test(f.name));
    const skipped = fileList.length - audioFiles.length;
    for (const file of audioFiles) {
      try {
        const row = await api.uploadFile(projectId, file);
        files.set(row.id, row);
        renderRows(files);
      } catch (error) {
        toast(`${file.name}: ${error.message}`);
      }
    }
    if (audioFiles.length) toast(t("project.uploaded", { count: audioFiles.length }));
    else if (skipped) toast(t("project.noAudio"));
  }

  el("file-upload").onchange = async (event) => {
    await uploadFiles(event.target.files);
    event.target.value = "";
  };

  setupDropZone(view, uploadFiles);

  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = () => el("file-upload")?.click();
  window.addEventListener("fab:click", fabHandler);

  el("server-import").onclick = () => openBrowser(async (paths) => {
    try {
      const imported = await api.importFiles(projectId, paths);
      for (const row of imported) files.set(row.id, row);
      renderRows(files);
      toast(t("project.imported", { count: imported.length }));
    } catch (error) {
      toast(error.message);
    }
  });

  el("transcribe-all").onclick = async () => {
    try {
      const jobs = [];
      for (const fileRow of files.values()) {
        if (fileRow.status === "done" || fileRow.status === "transcribing" || fileJobs.has(fileRow.id)) {
          continue;
        }
        jobs.push(await api.transcribeFile(fileRow.id, fileOptions(fileRow)));
      }
      if (!jobs.length) throw new Error(t("project.noFilesToTranscribe"));
      toast(t("project.jobsStarted", { count: jobs.length }));
    } catch (error) {
      toast(error.message);
    }
  };

  el("process-all").onclick = () => openAiDialog({ projectId });
  el("export-all").onclick = () => openExportDialog({ projectId });
  refreshExports();

  // automatic pipeline: transcription → cleanup (→ translation) without clicks
  el("auto-process").checked = Boolean(project.auto_process);
  fillLanguageSelect(el("auto-language"), {
    placeholder: t("project.autoCleanupOnly"),
    selected: project.auto_language ?? "",
    label: (name) => t("project.autoTranslateTo", { lang: name }),
  });
  async function saveAutoSettings() {
    try {
      await api.updateProject(projectId, {
        auto_process: el("auto-process").checked,
        auto_language: el("auto-language").value,
      });
      toast(t("project.autoSaved"));
    } catch (error) {
      toast(error.message);
    }
  }
  el("auto-process").onchange = saveAutoSettings;
  el("auto-language").onchange = saveAutoSettings;

  unsubscribers.forEach((off) => off());
  const jobEventsSeen = new Set(); // file_ids updated live while the snapshot loads
  unsubscribers = [
    on("file.update", (fileRow) => {
      if (fileRow.project_id !== projectId) return;
      files.set(fileRow.id, fileRow);
      renderRows(files);
    }),
    on("job.update", (job) => {
      if (job.kind === "export_pdf" && job.status === "done") {
        toast(t("export.done"));
        refreshExports();
      }
      if (job.file_id == null) return;
      jobEventsSeen.add(job.file_id);
      if (job.status === "queued" || job.status === "running") {
        fileJobs.set(job.file_id, job);
      } else {
        fileJobs.delete(job.file_id);
      }
      updateProgressRow(job);
      refreshQueuePositions();
    }),
    on("texts.changed", (data) => {
      if (openTextsRefresh) openTextsRefresh(data.file_id);
    }),
  ];

  // the jobs snapshot is taken only after subscribing, so no job.update can
  // fall between snapshot and subscription; WS events that arrived first win
  for (const job of await api.listJobs(true).catch(() => [])) {
    if (job.file_id != null && !jobEventsSeen.has(job.file_id)) {
      fileJobs.set(job.file_id, job);
    }
  }
  renderRows(files);

  // ── queue positions (multi-user fairness feedback) ────────────────
  let queueTimer = null;
  function refreshQueuePositions() {
    clearTimeout(queueTimer);
    queueTimer = setTimeout(async () => {
      const overview = await api.queueOverview().catch(() => null);
      if (!overview) return;
      const positions = new Map();
      for (const lane of Object.values(overview.lanes)) {
        for (const job of lane) {
          if (job.status === "queued" && job.file_id != null) {
            positions.set(job.file_id, job.queue_position);
          }
        }
      }
      for (const tr of el("file-rows")?.querySelectorAll("tr") ?? []) {
        const fileId = Number(tr.dataset.fileId);
        const message = tr.querySelector(".job-message");
        if (message && positions.has(fileId)) {
          message.textContent = t("project.queuePosition", { pos: positions.get(fileId) });
        }
      }
    }, 300);
  }

  function renderRows(fileMap) {
    const tbody = el("file-rows");
    if (!tbody) return;
    el("no-files").hidden = fileMap.size > 0;
    tbody.replaceChildren(...[...fileMap.values()].map((fileRow) => buildRow(fileRow)));
  }

  function buildRow(fileRow) {
    const tr = document.createElement("tr");
    tr.dataset.fileId = fileRow.id;

    const nameCell = document.createElement("td");
    nameCell.textContent = fileRow.filename;
    if (fileRow.status === "failed" && fileRow.error) {
      const err = document.createElement("div");
      err.className = "small error-text";
      err.textContent = fileRow.error;
      nameCell.appendChild(err);
    }

    const durationCell = document.createElement("td");
    durationCell.textContent = formatDuration(fileRow.duration);

    const languageCell = document.createElement("td");
    const languageSelect = document.createElement("select");
    languageSelect.className = "file-language";
    fillLanguageSelect(languageSelect, {
      placeholder: t("project.advAuto"),
      selected: fileLanguages.get(fileRow.id) ?? fileRow.language ?? "",
    });
    languageSelect.onchange = () => fileLanguages.set(fileRow.id, languageSelect.value);
    languageCell.appendChild(languageSelect);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge badge-${fileRow.status}`;
    badge.textContent = t(`status.${fileRow.status}`);
    statusCell.appendChild(badge);
    const job = fileJobs.get(fileRow.id);
    if (fileRow.status === "transcribing" || job) {
      const wrap = document.createElement("div");
      wrap.className = "progressbar small-bar";
      wrap.innerHTML = `<div style="width:${job?.progress ?? 0}%"></div>`;
      statusCell.appendChild(wrap);
      const message = document.createElement("div");
      message.className = "small muted job-message";
      message.textContent = job?.message ?? "";
      statusCell.appendChild(message);
    }

    const actionCell = document.createElement("td");
    actionCell.className = "col-actions";
    if (fileRow.status === "transcribing" || job) {
      actionCell.append(iconButton("stop", t("common.cancel"), async () => {
        const activeJob = fileJobs.get(fileRow.id);
        if (activeJob) await api.cancelJob(activeJob.id).catch((e) => toast(e.message));
      }));
    } else {
      actionCell.append(iconButton(
        fileRow.status === "done" ? "refresh" : "mic",
        fileRow.status === "done" ? t("project.again") : t("project.transcribe"),
        () => api.transcribeFile(fileRow.id, fileOptions(fileRow)).catch((e) => toast(e.message)),
      ));
    }
    if (fileRow.status === "done") {
      actionCell.append(iconButton("article", t("project.openEditor"), () => {
        location.hash = `#/editor/${fileRow.id}`;
      }));
      if (llmEnabled) {
        actionCell.append(
          iconButton("sparkle", t("ai.title"), () => openAiDialog({ fileId: fileRow.id }))
        );
      }
      actionCell.append(
        iconButton("pdf", t("export.file"), () => openExportDialog({ fileId: fileRow.id }))
      );
    }
    actionCell.append(iconButton("delete", t("common.delete"), async () => {
      await api.deleteFile(fileRow.id).catch((e) => toast(e.message));
      files.delete(fileRow.id);
      renderRows(files);
    }));

    tr.append(nameCell, languageCell, durationCell, statusCell, actionCell);
    return tr;
  }

  function fileOptions(fileRow) {
    const options = flowOptions();
    const language = fileLanguages.get(fileRow.id);
    return language ? { ...options, language } : options;
  }

  function updateProgressRow(job) {
    const tr = el("file-rows")?.querySelector(`tr[data-file-id="${job.file_id}"]`);
    if (!tr) return;
    const bar = tr.querySelector(".progressbar > div");
    const message = tr.querySelector(".job-message");
    if (bar) bar.style.width = `${job.progress}%`;
    if (message) message.textContent = job.message ?? "";
  }

  // ── AI processing dialog (cleanup / translation, per file or project) ──
  let openTextsRefresh = null;

  function openAiDialog({ fileId = null, projectId: forProject = null }) {
    const host = el("browser-modal");

    async function show() {
      const textsData = fileId ? await api.getTexts(fileId).catch(() => null) : null;
      host.replaceChildren(html`
        <div class="modal-backdrop">
          <div class="modal">
            <div class="modal-head">
              <strong>${t("ai.title")}</strong>
              <button class="text-btn small-btn" id="modal-close">${t("common.close")}</button>
            </div>
            <p class="small muted">${fileId ? t("ai.introFile") : t("ai.introProject")}</p>
            <label class="checkline">
              <input type="checkbox" id="ai-cleanup" checked> ${t("ai.cleanup")}
            </label>
            <label class="checkline">
              <input type="checkbox" id="ai-translate"> ${t("ai.translate")}
            </label>
            <select id="ai-language"></select>
            <div class="actions">
              <button id="ai-start">${t("ai.start")}</button>
            </div>
            <div id="ai-texts"></div>
          </div>
        </div>
      `);

      fillLanguageSelect(el("ai-language"), { selected: "en" });
      renderTexts(textsData);

      el("modal-close").onclick = close;
      host.querySelector(".modal-backdrop").onclick = (event) => {
        if (event.target === event.currentTarget) close();
      };
      el("ai-start").onclick = async () => {
        const steps = [];
        if (el("ai-cleanup").checked) steps.push("cleanup");
        if (el("ai-translate").checked) steps.push("translate");
        if (!steps.length) return;
        const options = { steps, target_language: el("ai-language").value };
        try {
          if (fileId) {
            await api.processFile(fileId, options);
            toast(t("ai.startedFile"));
          } else {
            const jobs = await api.processProject(forProject, options);
            toast(t("ai.startedProject", { count: jobs.length }));
            close();
          }
        } catch (error) {
          toast(error.message);
        }
      };
    }

    function renderTexts(textsData) {
      const container = el("ai-texts");
      if (!container || !textsData?.texts?.length) return;
      const tabs = document.createElement("div");
      tabs.className = "tabs";
      const body = document.createElement("pre");
      body.className = "text-view";
      for (const [index, text] of textsData.texts.entries()) {
        const tab = document.createElement("button");
        tab.className = "text-btn small-btn";
        tab.textContent = text.kind === "translation"
          ? `${t("ai.tabTranslation")} (${text.language})`
          : t("ai.tabCleanup");
        tab.onclick = () => {
          body.textContent = text.content;
          tabs.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
          tab.classList.add("active");
        };
        tabs.append(tab);
        if (index === 0) {
          body.textContent = text.content;
          tab.classList.add("active");
        }
      }
      container.replaceChildren(tabs, body);
    }

    openTextsRefresh = async (changedFileId) => {
      if (fileId && changedFileId === fileId && el("ai-texts")) {
        renderTexts(await api.getTexts(fileId).catch(() => null));
      }
    };

    function close() {
      openTextsRefresh = null;
      host.replaceChildren();
    }

    show();
  }

  // ── PDF export (template pipeline; per file or whole project) ────────

  function openExportDialog({ fileId = null, projectId: forProject = null }) {
    const host = el("browser-modal");
    host.replaceChildren(html`
      <div class="modal-backdrop">
        <div class="modal">
          <div class="modal-head">
            <strong>${t("export.title")}</strong>
            <button class="text-btn small-btn" id="modal-close">${t("common.close")}</button>
          </div>
          <p class="small muted">${fileId ? t("export.introFile") : t("export.introProject")}</p>
          <label for="export-language">${t("export.language")}</label>
          <select id="export-language"></select>
          <p class="hint">${t("export.hint")}</p>
          <div class="actions">
            <button id="export-start">${t("export.start")}</button>
          </div>
        </div>
      </div>
    `);

    fillLanguageSelect(el("export-language"), {
      placeholder: t("export.original"),
      label: (name) => t("export.translated", { lang: name }),
    });

    const close = () => host.replaceChildren();
    el("modal-close").onclick = close;
    host.querySelector(".modal-backdrop").onclick = (event) => {
      if (event.target === event.currentTarget) close();
    };
    el("export-start").onclick = async () => {
      const language = el("export-language").value;
      try {
        if (fileId) await api.exportFile(fileId, language);
        else await api.exportProject(forProject, language);
        toast(t("export.started"));
        close();
      } catch (error) {
        toast(error.message);
      }
    };
  }

  async function refreshExports() {
    const card = el("exports-card");
    const list = el("export-list");
    if (!card || !list) return;
    const exports = await api.listExports(projectId).catch(() => []);
    card.hidden = exports.length === 0;
    list.replaceChildren(...exports.map((entry) => {
      const row = document.createElement("div");
      row.className = "model-row";
      row.append(Object.assign(document.createElement("span"), {
        className: "model-name", textContent: entry.name,
      }));
      row.append(Object.assign(document.createElement("span"), {
        className: "muted small", textContent: `${(entry.size / 1024).toFixed(0)} kB`,
      }));
      row.append(Object.assign(document.createElement("span"), { className: "spacer" }));
      const download = document.createElement("a");
      download.className = "icon-btn";
      download.href = api.exportUrl(projectId, entry.name);
      download.title = t("export.download");
      download.setAttribute("aria-label", t("export.download"));
      download.innerHTML = iconSvg("download");
      row.append(download);
      row.append(iconButton("delete", t("common.delete"), async () => {
        await api.deleteExport(projectId, entry.name).catch((e) => toast(e.message));
        toast(t("export.deleted"));
        await refreshExports();
      }));
      return row;
    }));
  }

}

function fillAdvancedSelects(models) {
  const modelSelect = el("adv-model");
  const languageSelect = el("adv-language");
  if (!modelSelect || !languageSelect) return;

  const local = models.local ?? [];
  modelSelect.append(new Option(t("project.advDefault"), ""));
  for (const name of local) modelSelect.append(new Option(`📁 ${name}`, name));
  for (const name of models.builtin ?? []) {
    if (!local.includes(name)) modelSelect.append(new Option(name, name));
  }

  fillLanguageSelect(languageSelect, { placeholder: t("project.advAuto") });
}

// ── drag & drop upload (files and whole folders) ──────────────────────

function setupDropZone(zone, onFiles) {
  let depth = 0; // dragenter/-leave fire for every child element
  zone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    depth += 1;
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragover", (event) => event.preventDefault());
  zone.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) zone.classList.remove("dragover");
  });
  zone.addEventListener("drop", async (event) => {
    event.preventDefault();
    depth = 0;
    zone.classList.remove("dragover");
    onFiles(await collectDroppedFiles(event.dataTransfer));
  });
}

async function collectDroppedFiles(dataTransfer) {
  // webkitGetAsEntry must be read synchronously during the drop event
  const entries = [...(dataTransfer.items ?? [])]
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);
  if (!entries.length) return [...dataTransfer.files];

  const out = [];
  async function walk(entry) {
    if (entry.isFile) {
      out.push(await new Promise((resolve, reject) => entry.file(resolve, reject)));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        for (const child of batch) await walk(child);
      } while (batch.length);
    }
  }
  for (const entry of entries) await walk(entry);
  return out;
}

// ── server file browser (bottom sheet on mobile) ──────────────────────

function openBrowser(onImport) {
  const host = el("browser-modal");
  let currentPath = "";

  async function load(path) {
    let data;
    try {
      data = await api.browse(path);
    } catch (error) {
      toast(error.message);
      return;
    }
    currentPath = data.path;
    host.replaceChildren(html`
      <div class="modal-backdrop">
        <div class="modal">
          <div class="modal-head">
            <strong>${t("browser.title")}</strong>
            <button class="text-btn small-btn" id="modal-close">${t("common.close")}</button>
          </div>
          <p class="small muted modal-path">${data.path || t("browser.roots")}</p>
          <div class="browser-list" id="browser-list"></div>
          <div class="actions">
            <button id="import-folder" ${data.path ? "" : "disabled"}>${t("browser.importFolder")}</button>
          </div>
        </div>
      </div>
    `);

    const list = el("browser-list");
    if (data.parent !== null && data.parent !== undefined) {
      list.append(entryButton(`↩ ${t("browser.parent")}`, () => load(data.parent)));
    }
    for (const dir of data.dirs) {
      list.append(entryButton(`📁 ${dir.name}`, () => load(dir.path)));
    }
    for (const fileEntry of data.files) {
      list.append(entryButton(`🎵 ${fileEntry.name}`, () => {
        close();
        onImport([fileEntry.path]);
      }));
    }
    if (!data.dirs.length && !data.files.length) {
      const empty = document.createElement("p");
      empty.className = "muted small";
      empty.textContent = t("browser.empty");
      list.append(empty);
    }

    el("modal-close").onclick = close;
    el("import-folder").onclick = () => {
      close();
      onImport([currentPath]);
    };
    host.querySelector(".modal-backdrop").onclick = (event) => {
      if (event.target === event.currentTarget) close();
    };
  }

  function entryButton(label, onClick) {
    const button = document.createElement("button");
    button.className = "browser-entry";
    button.textContent = label;
    button.onclick = onClick;
    return button;
  }

  function close() {
    host.replaceChildren();
  }

  load("");
}
