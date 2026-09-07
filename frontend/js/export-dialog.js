// PDF export dialog — shared by the transcript view and the editor.
//
// Lives outside both views because the export is started from the file row as
// well as from the editor toolbar; the modal hangs in a host of its own at the
// end of <body> so it survives neither view's re-render.

import { api } from "./api.js";
import { el, html, toast } from "./dom.js";
import { t } from "./i18n.js";
import { fillLanguageSelect } from "./languages.js";

const COMBINED = "__combined__";
const HOST_ID = "export-modal-host";

function modalHost() {
  let host = el(HOST_ID);
  if (!host) {
    host = document.createElement("div");
    host.id = HOST_ID;
    document.body.appendChild(host);
  }
  return host;
}

export function closeExportDialog() {
  el(HOST_ID)?.replaceChildren();
}

/**
 * Ask for the language and start a PDF export — for one file, for a selection
 * of files or for a whole project. A selection of several files ends up in one
 * PDF, each file a section of it, exactly like the export of the whole
 * transcript; it just leaves the files out that were not selected.
 *
 * `onStarted` receives the enqueued job, so the caller can follow that one
 * export and nothing else — the editor downloads the PDF as soon as it is
 * finished, and only the export it started itself.
 */
export function openExportDialog({
  fileId = null, fileIds = null, projectId = null, onStarted = null,
} = {}) {
  const host = modalHost();
  const selection = fileIds ?? (fileId ? [fileId] : []);
  const intro = selection.length > 1
    ? t("export.introFiles", { count: selection.length })
    : (selection.length ? t("export.introFile") : t("export.introProject"));
  host.replaceChildren(html`
    <div class="modal-backdrop">
      <div class="modal">
        <div class="modal-head">
          <strong>${t("export.title")}</strong>
          <button class="text-btn small-btn" id="modal-close">${t("common.close")}</button>
        </div>
        <p class="small muted">${intro}</p>
        <label for="export-language">${t("export.language")}</label>
        <select id="export-language"></select>
        <p class="hint">${t("export.hint")}</p>
        <div class="actions">
          <button id="export-start">${t("export.start")}</button>
        </div>
      </div>
    </div>
  `);

  const select = el("export-language");
  fillLanguageSelect(select, {
    placeholder: t("export.original"),
    label: (name) => t("export.translated", { lang: name }),
  });
  // one PDF holding the original and every stored translation — right
  // below "original", not at the end of a hundred language entries
  select.add(new Option(t("export.combined"), COMBINED), 1);
  select.value = "";

  el("modal-close").onclick = closeExportDialog;
  host.querySelector(".modal-backdrop").onclick = (event) => {
    if (event.target === event.currentTarget) closeExportDialog();
  };
  el("export-start").onclick = async () => {
    const choice = el("export-language").value;
    const options = choice === COMBINED ? { combine: true } : { language: choice };
    try {
      let job;
      if (selection.length === 1) {
        job = await api.exportFile(selection[0], options);
      } else {
        // no selection: the whole transcript; a selection: the same job, told
        // which files belong in the one PDF it renders
        job = await api.exportProject(projectId, { ...options, file_ids: selection });
      }
      onStarted?.(job);
      toast(t("export.started"));
      closeExportDialog();
    } catch (error) {
      toast(error.message);
    }
  };
}
