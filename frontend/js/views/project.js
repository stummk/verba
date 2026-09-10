// Project view: import (upload, server browser, drag & drop), file cards with
// live progress, per-run advanced options (model/language), segment preview.

import { api } from "../api.js";
import { confirmAction, confirmDelete } from "../confirm.js";
import { el, esc, formatDuration, html, raw, toast } from "../dom.js";
import { closeExportDialog, openExportDialog } from "../export-dialog.js";
import { stepBadges } from "../file-steps.js";
import { iconButton, iconSvg } from "../icons.js";
import { labelHelp } from "../help.js";
import { t } from "../i18n.js";
import { jobCardHost, jobLine, jobStepLabel } from "../jobs.js";
import { languageChip, setChipLanguage } from "../language-chip.js";
import { fillLanguageSelect } from "../languages.js";
import { overflowMenu } from "../menu.js";
import { viewGuard } from "../navigation.js";
import { setChipType, typeChip } from "../type-chip.js";
import { on } from "../ws.js";

const AUDIO_RE = /\.(mp3|wav|m4a|flac|ogg|opus|aac|wma|webm|mp4)$/i;

let unsubscribers = [];

let fabHandler = null;
let queueTimerHandle = null;
const fileJobs = new Map(); // file_id -> latest active job
// file_id -> the last job that failed. A failed AI step leaves the file status
// at "done", so without this the card would give no hint at all — and the job
// itself says which of the three steps went wrong.
const fileJobErrors = new Map();
// file_id -> place in the queue, filled by refreshQueuePositions() and read by
// the badge tooltips, which are the only place a waiting file is explained now.
const fileQueuePositions = new Map();

// Who else can reach this transcript — shown next to its name, because
// "public" is not something to discover only when a colleague edits it.
function visibilityChip(project, authState) {
  if (!authState.enabled) return "";
  const owner = project.owner_name
    ? t("visibility.ownedBy", { name: project.owner_name })
    : t("visibility.ownerless");
  return raw(
    ` <span class="type-chip badge-visibility ${esc(project.visibility)}" title="${esc(owner)}">` +
    `${esc(t(`visibility.${project.visibility || "public"}`))}</span>`
  );
}

export async function render(view, _status, params) {
  // Two projects in a row are the same module, so the router does not call
  // destroy() in between — the previous render is released here instead.
  destroy();
  // A render this one overtook still has requests in flight, and its answers
  // would land here: a job that has long since finished then sits on a card as
  // a step still to be cancelled, until the next navigation clears the map.
  const stillMounted = viewGuard();
  const projectId = Number(params[0]);
  let projectError = null;
  const [project, settings, authState, types] = await Promise.all([
    api.getProject(projectId).catch((error) => {
      projectError = error;
      return null;
    }),
    api.getSettings().catch(() => null),
    api.authState().catch(() => ({ enabled: false, user: null })),
    // the picker behind every file's type chip — one list for the whole view
    api.listTypes().catch(() => []),
  ]);
  if (!stillMounted()) return;
  // The model list scans directories and probes the hardware — the view must
  // not wait for it, so the advanced dropdown fills in once it arrives.
  const modelsPromise = api.listModels().catch(() => ({ builtin: [], local: [] }));
  if (!project) {
    view.replaceChildren(html`<div class="card">${projectError.message}</div>`);
    return;
  }
  const llmEnabled = settings?.llm?.mode && settings.llm.mode !== "none";

  view.replaceChildren(html`
    <p><a href="#/" class="muted small">${t("project.back")}</a></p>
    <h1 class="view-title">
      <span class="view-title-text" title="${project.name}">${project.name}</span>${
        project.type_name
          ? raw(`<span class="type-chip" title="${esc(t("type.projectTitle", { name: project.type_name }))}">${esc(project.type_name)}</span>`)
          : ""
      }${visibilityChip(project, authState)}</h1>
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
        <div class="drop-zone" id="drop-zone">
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
          <p class="hint drop-hint">${t("project.dropHint")}</p>
        </div>
        <p class="hint">${t("project.workspaceHint", { path: project.workspace })}</p>
      </div>
      <div class="step-panel" id="step-panel-2" role="tabpanel" hidden>
        <div class="step-actions">
          <button id="transcribe-all" class="icon-label">
            ${raw(iconSvg("speechToText"))} ${t("project.transcribeAll")}
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
    <div id="upload-progress" hidden></div>
    <details class="card collapse-card" id="exports-card" open>
      <summary>
        <span>${t("export.exports")}</span>
        <span class="spacer"></span>
        <span id="project-jobs" hidden></span>
      </summary>
      <div class="collapse-body">
        <div class="bulk-bar" id="export-bulk" hidden>
          <span id="export-bulk-count"></span>
          <span class="spacer"></span>
          <button type="button" class="tonal small-btn icon-label" id="export-bulk-download">
            ${raw(iconSvg("download"))} ${t("export.download")}
          </button>
          <button type="button" class="danger small-btn icon-label" id="export-bulk-delete">
            ${raw(iconSvg("delete"))} ${t("common.delete")}
          </button>
        </div>
        <div id="export-list"></div>
        <p class="muted small" id="no-exports" hidden>${t("export.none")}</p>
      </div>
    </details>
    <details class="card collapse-card" id="files-card" open>
      <summary><span>${t("project.files")}</span></summary>
      <div class="collapse-body">
        <div class="bulk-bar" id="file-bulk" hidden>
          <span id="file-bulk-count"></span>
          <span class="spacer"></span>
          <button type="button" class="tonal small-btn icon-label" id="bulk-transcribe">
            ${raw(iconSvg("speechToText"))} ${t("project.transcribe")}
          </button>
          <button type="button" class="tonal small-btn icon-label" id="bulk-export">
            ${raw(iconSvg("pdf"))} ${t("export.file")}
          </button>
          <button type="button" class="danger small-btn icon-label" id="bulk-delete">
            ${raw(iconSvg("delete"))} ${t("common.delete")}
          </button>
        </div>
        <label class="checkline select-all-line" id="select-all-line">
          <input type="checkbox" id="file-select-all">
          <span class="muted small">${t("project.selectAll")}</span>
        </label>
        <div class="file-cards" id="file-rows"></div>
        <p class="muted small" id="no-files" hidden>${t("project.noFiles")}</p>
      </div>
    </details>
    <div id="browser-modal"></div>
  `);

  fillLanguageSelect(el("adv-language"), { placeholder: t("project.advAuto") });
  modelsPromise.then((models) => {
    if (stillMounted() && el("adv-model")) fillModelSelect(models);
  });

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
  // What the bar above the table acts on. Survives a re-render (a row is
  // rebuilt on every job event), and forgets a file that is gone.
  const selectedFiles = new Set();
  const selectedExports = new Set();
  fileJobs.clear();
  fileJobErrors.clear();
  fileQueuePositions.clear();
  renderRows(files);

  function flowOptions() {
    return {
      model: el("adv-model")?.value ?? "",
      language: el("adv-language")?.value ?? "",
    };
  }

  // An upload has no job behind it, so nothing in the UI would move while a
  // large file (or a whole folder) goes over the wire — hence its own card.
  const uploadProgress = uploadProgressCard(el("upload-progress"));

  let uploading = false;

  async function uploadFiles(fileList) {
    if (uploading) { // a second drop while the first batch is still going up
      toast(t("project.uploadBusy"));
      return;
    }
    uploading = true;
    const audioFiles = [...fileList].filter((f) => AUDIO_RE.test(f.name));
    const skipped = fileList.length - audioFiles.length;
    const totalBytes = audioFiles.reduce((sum, f) => sum + f.size, 0);
    let sentBytes = 0;
    let uploaded = 0;
    uploadProgress.start(audioFiles.length);
    try {
      for (const [index, file] of audioFiles.entries()) {
        uploadProgress.update({
          index: index + 1,
          name: file.name,
          bytes: sentBytes,
          totalBytes,
        });
        try {
          const row = await api.uploadFile(projectId, file, (loaded) => {
            uploadProgress.update({
              index: index + 1,
              name: file.name,
              bytes: sentBytes + loaded,
              totalBytes,
              // the browser is done, the server still stores and probes the file
              processing: loaded >= file.size,
            });
          });
          sentBytes += file.size;
          uploaded += 1;
          files.set(row.id, row);
          renderRows(files);
        } catch (error) {
          sentBytes += file.size;
          toast(`${file.name}: ${error.message}`);
        }
      }
    } finally {
      uploading = false;
      uploadProgress.stop();
    }
    if (uploaded) toast(t("project.uploaded", { count: uploaded }));
    else if (skipped && !audioFiles.length) toast(t("project.noAudio"));
  }

  el("file-upload").onchange = async (event) => {
    await uploadFiles(event.target.files);
    event.target.value = "";
  };

  // bound to the import panel, never to #view: that element outlives every
  // render, so listeners on it would pile up and keep uploading into the
  // project of an earlier render — from any view
  setupDropZone(el("drop-zone"), uploadFiles);

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
    const busy = (fileRow) => fileRow.status === "transcribing" || fileJobs.has(fileRow.id);
    const rows = [...files.values()].filter((fileRow) => !busy(fileRow));
    // Pressed in front of a list where everything is already transcribed, the
    // button used to answer "no files" — which reads as if the list were empty.
    // What is meant then is a second run, so that is what it offers.
    const open = rows.filter((fileRow) => fileRow.status !== "done");
    await startTranscriptions(
      open.length ? open : rows,
      files.size ? t("project.alreadyRunning") : t("project.noFiles"),
    );
  };

  // The one place both bulk buttons agree on: a run over files that are
  // already transcribed replaces their segments — and every manual edit in
  // them — so it asks once before it starts. A single row's "Erneut" button
  // stays as it is: one file is a deliberate choice, not a sweep.
  async function startTranscriptions(rows, emptyMessage) {
    if (!rows.length) {
      toast(emptyMessage);
      return;
    }
    const again = rows.filter((fileRow) => fileRow.status === "done").length;
    if (again) {
      const ok = await confirmAction({
        title: t("project.retranscribeTitle"),
        message: t("project.retranscribeConfirm", { count: again }),
        confirmLabel: t("project.retranscribeStart"),
      });
      if (!ok) return;
    }
    let started = 0;
    for (const fileRow of rows) {
      try {
        await api.transcribeFile(fileRow.id, fileOptions(fileRow));
        started += 1;
      } catch (error) {
        toast(`${fileRow.filename}: ${error.message}`);
      }
    }
    if (started) toast(t("project.jobsStarted", { count: started }));
  }

  el("process-all").onclick = () => openAiDialog({ projectId });
  el("export-all").onclick = () => openExportDialog({ projectId });
  refreshExports();

  // ── the selection in the file table ─────────────────────────────────
  el("file-select-all").onchange = (event) => {
    selectedFiles.clear();
    if (event.target.checked) for (const id of files.keys()) selectedFiles.add(id);
    renderRows(files);
  };

  // the bulk bar only exists with a selection, so an empty result means the
  // selected files are all running already
  el("bulk-transcribe").onclick = () => startTranscriptions(
    selectedRows().filter(
      (fileRow) => fileRow.status !== "transcribing" && !fileJobs.has(fileRow.id)
    ),
    t("project.alreadyRunning"),
  );

  el("bulk-export").onclick = () => {
    // only a transcribed file has anything to put into a PDF
    const ready = selectedRows().filter((fileRow) => fileRow.status === "done");
    if (!ready.length) {
      toast(t("project.noFilesToExport"));
      return;
    }
    if (ready.length < selectedFiles.size) {
      toast(t("project.exportSkipped", { count: selectedFiles.size - ready.length }));
    }
    openExportDialog({ projectId, fileIds: ready.map((fileRow) => fileRow.id) });
  };

  el("bulk-delete").onclick = async () => {
    const rows = selectedRows();
    if (!rows.length) return;
    const ok = await confirmDelete({
      message: rows.length === 1
        ? t("project.deleteFileConfirm", { name: rows[0].filename })
        : t("project.deleteFilesConfirm", { count: rows.length }),
    });
    if (!ok) return;
    let deleted = 0;
    for (const fileRow of rows) {
      try {
        await api.deleteFile(fileRow.id);
        files.delete(fileRow.id);
        deleted += 1;
      } catch (error) {
        toast(`${fileRow.filename}: ${error.message}`);
      }
    }
    renderRows(files);
    if (deleted) toast(t("project.filesDeleted", { count: deleted }));
  };

  function selectedRows() {
    return [...selectedFiles].map((id) => files.get(id)).filter(Boolean);
  }

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
  // jobs of the whole transcript (PDF export of all files) have no file row to
  // live in — they run in the heading of the export section, which stays
  // readable whether the section is open or closed
  const projectJobs = jobCardHost(el("project-jobs"), {
    filter: (job) => job.project_id === projectId && job.file_id == null,
    item: jobLine,
    onCancel: (job) => api.cancelJob(job.id).catch((error) => toast(error.message)),
  });
  const jobEventsSeen = new Set(); // file_ids updated live while the snapshot loads
  unsubscribers = [
    on("file.update", (fileRow) => {
      if (fileRow.project_id !== projectId) return;
      files.set(fileRow.id, fileRow);
      renderRows(files);
    }),
    on("job.update", (job) => {
      projectJobs.apply(job);
      if (job.kind === "export_pdf" && job.status === "done") {
        toast(t("export.done"));
        refreshExports();
      }
      if (job.file_id == null) return;
      jobEventsSeen.add(job.file_id);
      if (job.status === "queued" || job.status === "running") {
        fileJobs.set(job.file_id, job);
        fileJobErrors.delete(job.file_id);
      } else {
        fileJobs.delete(job.file_id);
        if (job.status === "failed" && job.error) fileJobErrors.set(job.file_id, job);
      }
      updateProgressRow(job);
      refreshQueuePositions();
    }),
    on("texts.changed", (data) => {
      // the file status does not change with a derived text, so the row has to
      // learn about it from this event — which carries the kinds the file has
      // left, so an emptied or deleted text takes its chip off the row again
      const fileRow = files.get(data.file_id);
      if (fileRow) {
        fileRow.derived_kinds = data.kinds ?? "";
        renderRows(files);
      }
    }),
  ];

  // the jobs snapshot is taken only after subscribing, so no job.update can
  // fall between snapshot and subscription; WS events that arrived first win
  const jobSnapshot = await api.listJobs(true).catch(() => []);
  if (!stillMounted()) return; // the snapshot is as old as its request
  for (const job of jobSnapshot) {
    projectJobs.apply(job);
    if (job.file_id != null && !jobEventsSeen.has(job.file_id)) {
      fileJobs.set(job.file_id, job);
    }
  }
  renderRows(files);

  // ── queue positions (multi-user fairness feedback) ────────────────
  function refreshQueuePositions() {
    clearTimeout(queueTimerHandle);
    queueTimerHandle = setTimeout(async () => {
      const overview = await api.queueOverview().catch(() => null);
      if (!overview || !stillMounted()) return;
      const positions = new Map();
      for (const lane of Object.values(overview.lanes)) {
        for (const job of lane) {
          if (job.status === "queued" && job.file_id != null) {
            positions.set(job.file_id, job.queue_position);
          }
        }
      }
      fileQueuePositions.clear();
      for (const [fileId, position] of positions) fileQueuePositions.set(fileId, position);
      // the position is only ever visible in a badge tooltip, so only the
      // badges of the waiting files have to be redrawn
      for (const card of el("file-rows")?.querySelectorAll(".file-card") ?? []) {
        const fileId = Number(card.dataset.fileId);
        const fileRow = files.get(fileId);
        if (fileRow && positions.has(fileId)) refreshCardSteps(card, fileRow);
      }
    }, 300);
  }

  function renderRows(fileMap) {
    const host = el("file-rows");
    if (!host) return;
    el("no-files").hidden = fileMap.size > 0;
    el("select-all-line").hidden = fileMap.size === 0;
    for (const id of [...selectedFiles]) {
      if (!fileMap.has(id)) selectedFiles.delete(id); // deleted elsewhere
    }
    host.replaceChildren(...[...fileMap.values()].map((fileRow) => buildRow(fileRow)));
    refreshFileSelection();
  }

  // The bar only exists while something is selected — an empty one would just
  // take a line away from the table.
  function refreshFileSelection() {
    const bar = el("file-bulk");
    if (!bar) return;
    bar.hidden = selectedFiles.size === 0;
    el("file-bulk-count").textContent = t("project.selected", { count: selectedFiles.size });
    const all = el("file-select-all");
    all.checked = files.size > 0 && selectedFiles.size === files.size;
    all.indeterminate = selectedFiles.size > 0 && selectedFiles.size < files.size;
  }

  // One file, one card, and the whole card is the way into the editor — that
  // is what the list is for. The link is a transparent layer over the card
  // rather than a wrapper around it: a checkbox or a button inside an <a> can
  // only be kept from navigating by cancelling the click, which also cancels
  // the checkbox's own tick. So every control sits *above* that layer instead
  // and keeps its plain behaviour.
  function buildRow(fileRow) {
    const card = document.createElement("div");
    card.className = "card file-card";
    card.dataset.fileId = fileRow.id;
    const overlay = document.createElement("a");
    overlay.className = "file-card-overlay";
    overlay.href = `#/editor/${fileRow.id}`;
    overlay.title = t("project.openEditor");
    overlay.setAttribute("aria-label", `${fileRow.filename} — ${t("project.openEditor")}`);
    card.append(overlay);
    // The badges and the failure text have to sit above that layer, or they
    // would never get a hover and never show their tooltip — which is where
    // their whole text lives. So the card answers a click on them itself, and
    // the whole card stays one surface leading into the editor.
    card.addEventListener("click", (event) => {
      if (event.target.closest("a, button, input")) return;
      location.hash = `#/editor/${fileRow.id}`;
    });

    const select = document.createElement("input");
    select.type = "checkbox";
    select.className = "row-select file-card-select";
    select.checked = selectedFiles.has(fileRow.id);
    select.title = t("project.selectFile");
    select.setAttribute("aria-label", t("project.selectFile"));
    select.onchange = () => {
      if (select.checked) selectedFiles.add(fileRow.id);
      else selectedFiles.delete(fileRow.id);
      card.classList.toggle("selected", select.checked);
      refreshFileSelection();
    };
    card.classList.toggle("selected", select.checked);

    const title = Object.assign(document.createElement("div"), {
      className: "file-card-title", textContent: fileRow.filename, title: fileRow.filename,
    });

    // Below the name the two facts about the recording itself: which language
    // it is transcribed in (and can be changed to) and how long it runs.
    const meta = document.createElement("div");
    meta.className = "file-card-meta";
    const chipOptions = {
      hint: t("project.advLanguage"),
      autoTitle: t("project.advAuto"),
    };
    const languageNode = languageChip({
      ...chipOptions,
      code: fileLanguages.get(fileRow.id) ?? fileRow.language ?? "",
      dialogTitle: t("project.advLanguage"),
      onPick: (code) => {
        fileLanguages.set(fileRow.id, code);
        setChipLanguage(languageNode, code, chipOptions);
      },
    });
    meta.append(languageNode);
    // Which type this file is transcribed as: the project's, or one of its
    // own. The chip says which of the two it is and opens the picker.
    meta.append(typeChip({
      typeId: fileRow.type_id ?? null,
      types,
      projectTypeName: project.type_name ?? "",
      onPick: async (typeId) => {
        try {
          const updated = await api.updateFileType(fileRow.id, typeId);
          files.set(updated.id, updated);
          const chip = card.querySelector(".file-type-chip");
          if (chip) {
            setChipType(chip, updated.type_id ?? null, {
              types, projectTypeName: project.type_name ?? "",
            });
          }
        } catch (error) {
          toast(error.message);
        }
      },
    }));
    const duration = document.createElement("span");
    duration.className = "file-card-duration muted small";
    duration.title = t("project.colDuration");
    duration.innerHTML = iconSvg("schedule");
    duration.append(Object.assign(document.createElement("span"), {
      textContent: formatDuration(fileRow.duration),
    }));
    meta.append(duration);
    // The badges close the meta line on the right, so they keep their place
    // whatever happens in the corner above them: the stop button comes and
    // goes with a running step, and it must not shove them around.
    meta.append(stepBadges(fileRow, badgeContext(fileRow)));

    const actions = document.createElement("div");
    actions.className = "file-card-actions";
    // A running step is stopped from the card, not from the menu: it is the
    // one action that is urgent, and it only exists while something runs.
    const stop = iconButton("stop", t("common.cancel"), async () => {
      const activeJob = fileJobs.get(fileRow.id);
      if (activeJob) await api.cancelJob(activeJob.id).catch((e) => toast(e.message));
    });
    stop.classList.add("file-card-stop");
    stop.hidden = !isBusy(fileRow);
    actions.append(stop, fileMenu(fileRow));

    card.append(select, title, actions, meta);
    refreshCardFailure(card, fileRow);
    return card;
  }

  // What the card shows and what its menu offers both depend on whether the
  // file is working on something right now.
  function isBusy(fileRow) {
    return fileRow.status === "transcribing" || fileJobs.has(fileRow.id);
  }

  function badgeContext(fileRow) {
    const job = fileJobs.get(fileRow.id) ?? null;
    return {
      job,
      failed: fileJobErrors.get(fileRow.id) ?? null,
      detail: job ? jobDetail(job, fileRow) : "",
      queuePosition: job?.status === "queued"
        ? (fileQueuePositions.get(fileRow.id) ?? null)
        : null,
    };
  }

  // The actions of a file, in the order of the workflow: transcribe → AI
  // processing → check in the editor → export → delete. Built on every open,
  // because a file that has just finished transcribing offers more than the
  // same file did a minute ago.
  function fileMenu(fileRow) {
    return overflowMenu({
      label: t("project.fileMenu"),
      items: () => {
        const current = files.get(fileRow.id) ?? fileRow;
        const busy = isBusy(current);
        const job = fileJobs.get(current.id);
        return [
          !busy && {
            icon: current.status === "done" ? "refresh" : "speechToText",
            label: current.status === "done" ? t("project.again") : t("project.transcribe"),
            onSelect: () => api.transcribeFile(current.id, fileOptions(current))
              .catch((e) => toast(e.message)),
          },
          busy && {
            icon: "stop",
            label: t("common.cancel"),
            onSelect: () => {
              if (job) api.cancelJob(job.id).catch((e) => toast(e.message));
            },
          },
          current.status === "done" && llmEnabled && job?.kind !== "llm_process" && {
            icon: "sparkle",
            label: t("ai.title"),
            onSelect: () => openAiDialog({ fileId: current.id }),
          },
          {
            icon: "editNote",
            label: t("project.openEditor"),
            onSelect: () => { location.hash = `#/editor/${current.id}`; },
          },
          current.status === "done" && {
            icon: "pdf",
            label: t("export.file"),
            onSelect: () => openExportDialog({ fileId: current.id }),
          },
          {
            icon: "delete",
            label: t("common.delete"),
            danger: true,
            onSelect: async () => {
              const ok = await confirmDelete({
                message: t("project.deleteFileConfirm", { name: current.filename }),
              });
              if (!ok) return;
              try {
                await api.deleteFile(current.id);
                files.delete(current.id);
                renderRows(files);
              } catch (error) {
                toast(error.message);
              }
            },
          },
        ];
      },
    });
  }

  function fileOptions(fileRow) {
    const options = flowOptions();
    const language = fileLanguages.get(fileRow.id);
    return language ? { ...options, language } : options;
  }

  // A job event repaints only the badges and the stop button of its card. The
  // card is not rebuilt: that would throw away the language chip mid-pick and
  // close an open menu on every progress tick.
  function updateProgressRow(job) {
    const card = el("file-rows")?.querySelector(`.file-card[data-file-id="${job.file_id}"]`);
    const fileRow = files.get(job.file_id);
    if (!card || !fileRow) return;
    refreshCardSteps(card, fileRow);
  }

  function refreshCardSteps(card, fileRow) {
    card.querySelector(".file-card-steps")?.replaceWith(
      stepBadges(fileRow, badgeContext(fileRow))
    );
    const stop = card.querySelector(".file-card-stop");
    if (stop) stop.hidden = !isBusy(fileRow);
    // a failed AI step leaves the file status at "done", so no file.update
    // rebuilds the card — the reason has to arrive with the job event
    refreshCardFailure(card, fileRow);
  }

  // Why the last step did not work, in the third row of the card. The node is
  // created and removed with the failure, so a successful retry clears it.
  function refreshCardFailure(card, fileRow) {
    const failure = fileRow.status === "failed"
      ? fileRow.error
      : fileJobErrors.get(fileRow.id)?.error;
    const existing = card.querySelector(".file-card-error");
    if (!failure) {
      existing?.remove();
      return;
    }
    const err = existing ?? document.createElement("div");
    err.className = "small error-text file-card-error";
    err.textContent = failure;
    err.title = failure; // the card clamps long endpoint messages
    if (!existing) card.append(err);
  }

  // What a running job is doing right now — "Bereinigung 2/5". It lands in the
  // tooltip of the badge, which already names the step, so neither the step
  // nor the file name is repeated here; the message carries both otherwise.
  function jobDetail(job, fileRow) {
    const filename = fileRow?.filename ?? "";
    const detail = (job.message ?? "").replace(`${filename}:`, "").trim();
    return detail || jobStepLabel(job);
  }

  // ── AI processing dialog (cleanup / translation, per file or project) ──
  function openAiDialog({ fileId = null, projectId: forProject = null }) {
    const host = el("browser-modal");

    function show() {
      host.replaceChildren(html`
        <div class="modal-backdrop">
          <div class="modal">
            <div class="modal-head">
              ${raw(labelHelp(
                `<strong>${t("ai.title")}</strong>`,
                fileId ? t("ai.introFile") : t("ai.introProject"), "inline-row"
              ))}
              <button class="text-btn small-btn" id="modal-close">${t("common.close")}</button>
            </div>
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
          </div>
        </div>
      `);

      fillLanguageSelect(el("ai-language"), { selected: "en" });

      el("modal-close").onclick = close;
      host.querySelector(".modal-backdrop").onclick = (event) => {
        if (event.target === event.currentTarget) close();
      };
      el("ai-start").onclick = async (event) => {
        const steps = [];
        if (el("ai-cleanup").checked) steps.push("cleanup");
        if (el("ai-translate").checked) steps.push("translate");
        if (!steps.length) return;
        const options = { steps, target_language: el("ai-language").value };
        // The dialog closes on start and the progress appears in the file row:
        // a dialog that stays open reads as "nothing happened" and the next
        // click queues the same work a second time.
        event.currentTarget.disabled = true;
        try {
          if (fileId) {
            await api.processFile(fileId, options);
            toast(t("ai.startedFile"));
          } else {
            const jobs = await api.processProject(forProject, options);
            toast(t("ai.startedProject", { count: jobs.length }));
          }
          close();
        } catch (error) {
          toast(error.message);
          const button = el("ai-start");
          if (button) button.disabled = false;
        }
      };
    }

    function close() {
      host.replaceChildren();
    }

    show();
  }

  // ── PDF export (template pipeline; per file or whole project) ────────

  async function refreshExports() {
    const list = el("export-list");
    if (!list) return;
    const exports = await api.listExports(projectId).catch(() => []);
    el("no-exports").hidden = exports.length > 0;
    const names = new Set(exports.map((entry) => entry.name));
    for (const name of [...selectedExports]) {
      if (!names.has(name)) selectedExports.delete(name); // deleted meanwhile
    }
    list.replaceChildren(...exports.map((entry) => {
      const row = document.createElement("div");
      row.className = "model-row";
      const select = document.createElement("input");
      select.type = "checkbox";
      select.className = "row-select";
      select.checked = selectedExports.has(entry.name);
      select.title = t("export.selectFile");
      select.setAttribute("aria-label", t("export.selectFile"));
      select.onchange = () => {
        if (select.checked) selectedExports.add(entry.name);
        else selectedExports.delete(entry.name);
        refreshExportSelection();
      };
      row.append(select);
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
        const ok = await confirmDelete({
          message: t("export.deleteConfirm", { name: entry.name }),
        });
        if (!ok) return;
        try {
          await api.deleteExport(projectId, entry.name);
          toast(t("export.deleted"));
        } catch (error) {
          toast(error.message);
        }
        await refreshExports();
      }));
      return row;
    }));
    refreshExportSelection();
  }

  function refreshExportSelection() {
    const bar = el("export-bulk");
    if (!bar) return;
    bar.hidden = selectedExports.size === 0;
    el("export-bulk-count").textContent = t("project.selected", { count: selectedExports.size });
  }

  // Several PDFs leave as one zip: a browser asks about a single download
  // instead of blocking the second and third one as a popup.
  el("export-bulk-download").onclick = () => {
    const names = [...selectedExports];
    if (!names.length) return;
    const link = document.createElement("a");
    link.href = names.length === 1
      ? api.exportUrl(projectId, names[0])
      : api.exportsZipUrl(projectId, names);
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  el("export-bulk-delete").onclick = async () => {
    const names = [...selectedExports];
    if (!names.length) return;
    const ok = await confirmDelete({
      message: names.length === 1
        ? t("export.deleteConfirm", { name: names[0] })
        : t("export.deleteConfirmCount", { count: names.length }),
    });
    if (!ok) return;
    let deleted = 0;
    for (const name of names) {
      try {
        await api.deleteExport(projectId, name);
        deleted += 1;
      } catch (error) {
        toast(`${name}: ${error.message}`);
      }
    }
    selectedExports.clear();
    await refreshExports();
    if (deleted) toast(t("export.deletedCount", { count: deleted }));
  };

}

function fillModelSelect(models) {
  const modelSelect = el("adv-model");
  if (!modelSelect) return;

  const local = models.local ?? [];
  modelSelect.replaceChildren();
  modelSelect.append(new Option(t("project.advDefault"), ""));
  for (const name of local) modelSelect.append(new Option(`📁 ${name}`, name));
  for (const name of models.builtin ?? []) {
    if (!local.includes(name)) modelSelect.append(new Option(name, name));
  }
}

// ── upload progress card ──────────────────────────────────────────────

// The percentage counts bytes over the whole selection, so one bar covers a
// single file as well as a dropped folder with fifty of them.
function uploadProgressCard(host) {
  let total = 0;

  return {
    start(count) {
      total = count;
      if (!count) return;
      host.replaceChildren(html`
        <div class="card job-card">
          <div class="job-card-head">
            <strong>${t("project.uploading")}</strong>
            <span class="spacer"></span>
            <span class="small muted" id="upload-percent">0 %</span>
          </div>
          <div class="progressbar small-bar"><div id="upload-bar"></div></div>
          <div class="small muted job-message" id="upload-message"></div>
        </div>
      `);
      host.hidden = false;
      host.scrollIntoView({ block: "nearest" });
    },

    update({ index, name, bytes, totalBytes, processing = false }) {
      if (host.hidden) return;
      const percent = totalBytes ? Math.min(100, Math.round((bytes / totalBytes) * 100)) : 0;
      el("upload-bar").style.width = `${percent}%`;
      el("upload-percent").textContent = `${percent} %`;
      el("upload-message").textContent = processing
        ? t("project.uploadProcessing", { name })
        : t("project.uploadFile", { index, total, name });
    },

    stop() {
      host.replaceChildren();
      host.hidden = true;
    },
  };
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

// Called by the router when another view takes over. Without this the file
// and job subscriptions of a project kept redrawing rows that are no longer
// on screen — and the drop zone of an old render kept accepting uploads.
export function destroy() {
  closeExportDialog(); // the dialog hangs on <body> and would outlive the view
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  clearTimeout(queueTimerHandle);
  fileJobs.clear();
  fileJobErrors.clear();
  fileQueuePositions.clear();
  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = null;
}
