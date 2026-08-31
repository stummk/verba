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

/** Ask for the language and start a PDF export — for one file or a project. */
export function openExportDialog({ fileId = null, projectId = null } = {}) {
  const host = modalHost();
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
      if (fileId) await api.exportFile(fileId, options);
      else await api.exportProject(projectId, options);
      toast(t("export.started"));
      closeExportDialog();
    } catch (error) {
      toast(error.message);
    }
  };
}
