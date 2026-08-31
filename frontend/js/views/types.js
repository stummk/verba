// Transcript type management: master-detail — list (selection) plus edit pane.
// Mobile shows one pane at a time; the FAB creates a new type.

import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { iconButton } from "../icons.js";
import { t } from "../i18n.js";

let fabHandler = null;
let types = [];
let selected = null; // a type object, "new", or null
let defaults = { output_prompt: "", structure: "paragraphs", structures: ["paragraphs"] };
// both prompts of the type being edited, so switching the dropdown keeps
// unsaved edits of the other one
let draft = { system_prompt: "", output_prompt: "", structure: "paragraphs" };
let promptKind = "system_prompt";

export async function render(view) {
  types = await api.listTypes();
  defaults = await api.typeDefaults().catch(() => defaults);

  view.replaceChildren(html`
    <h1>${t("types.title")}</h1>
    <p class="muted small">${t("types.intro")}</p>
    <div class="types-layout" id="types-layout">
      <div class="card types-list-pane">
        <div id="types-list"></div>
        <div class="actions">
          <button type="button" class="text-btn" id="type-restore">${t("types.restore")}</button>
        </div>
      </div>
      <div class="card types-detail-pane" id="types-detail"></div>
    </div>
  `);

  el("type-restore").onclick = async () => {
    try {
      await api.restoreTypes();
      toast(t("types.restored"));
      await reload();
    } catch (error) {
      toast(error.message);
    }
  };

  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = () => {
    if (currentRoute() === "types") select("new");
  };
  window.addEventListener("fab:click", fabHandler);

  select(null);
}

function currentRoute() {
  return location.hash.replace(/^#\/?/, "").split("/")[0] || "dashboard";
}

async function reload(keepId = null) {
  types = await api.listTypes();
  select(keepId === null ? null : (types.find((type) => type.id === keepId) ?? null));
}

function select(target) {
  selected = target;
  promptKind = "system_prompt";
  const isNew = target === "new";
  draft = {
    system_prompt: isNew ? "" : (target?.system_prompt ?? ""),
    // a new type starts from the default output prompt so it can be adapted
    output_prompt: isNew ? defaults.output_prompt : (target?.output_prompt ?? ""),
    structure: (isNew ? defaults.structure : target?.structure) || defaults.structure,
  };
  renderList();
  renderDetail();
}

function renderList() {
  const host = el("types-list");
  if (!host) return;
  host.replaceChildren(...types.map((type) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "type-row";
    row.classList.toggle("selected", selected !== "new" && selected?.id === type.id);
    row.append(Object.assign(document.createElement("span"), {
      className: "model-name", textContent: type.name,
    }));
    if (type.builtin) {
      row.append(Object.assign(document.createElement("span"), {
        className: "badge", textContent: t("types.builtin"),
      }));
    }
    row.onclick = () => select(type);
    return row;
  }));
  el("types-layout")?.classList.toggle("show-detail", selected !== null);
}

function renderDetail() {
  const pane = el("types-detail");
  if (!pane) return;

  if (selected === null) {
    pane.replaceChildren(html`<p class="panel-empty">${t("types.select")}</p>`);
    return;
  }

  const isNew = selected === "new";
  const type = isNew ? null : selected;
  pane.replaceChildren(html`
    <button type="button" class="text-btn types-back" id="types-back">${t("types.backToList")}</button>
    <div class="types-detail-head">
      <h2>${isNew ? t("types.add") : type.name}</h2>
      <span class="spacer"></span>
      <span id="type-delete-slot"></span>
    </div>
    <label for="type-name">${t("types.name")}</label>
    <input id="type-name" maxlength="100" autocomplete="off">
    <label for="type-structure">${t("types.structure")}</label>
    <select id="type-structure"></select>
    <p class="hint">${t("types.structureHint")}</p>
    <label for="type-prompt-kind">${t("types.promptKind")}</label>
    <select id="type-prompt-kind">
      <option value="system_prompt">${t("types.promptCleanup")}</option>
      <option value="output_prompt">${t("types.promptOutput")}</option>
    </select>
    <textarea id="type-prompt" class="type-prompt" maxlength="8000"
      aria-label="${t("types.prompt")}"></textarea>
    <p class="hint" id="type-prompt-hint"></p>
    <div class="actions">
      <button type="button" id="type-save">${t("common.save")}</button>
      <button type="button" class="text-btn" id="type-prompt-default"
        hidden>${t("types.promptUseDefault")}</button>
    </div>
  `);

  el("type-name").value = type?.name ?? "";
  el("types-back").onclick = () => select(null);

  const structureSelect = el("type-structure");
  structureSelect.replaceChildren(...defaults.structures.map((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = structureLabels()[value] ?? value;
    return option;
  }));
  structureSelect.value = draft.structure;
  structureSelect.onchange = () => { draft.structure = structureSelect.value; };

  const kindSelect = el("type-prompt-kind");
  const textarea = el("type-prompt");
  kindSelect.value = promptKind;
  kindSelect.onchange = () => {
    draft[promptKind] = textarea.value; // keep the edits of the one we leave
    promptKind = kindSelect.value;
    showPrompt();
  };
  textarea.oninput = () => { draft[promptKind] = textarea.value; };
  el("type-prompt-default").onclick = () => {
    draft.output_prompt = defaults.output_prompt;
    showPrompt();
    textarea.focus();
  };
  showPrompt();

  if (!isNew) {
    el("type-delete-slot").append(iconButton("delete", t("common.delete"), async () => {
      try {
        await api.deleteType(type.id);
        toast(t("types.deleted"));
        await reload();
      } catch (error) {
        toast(error.message);
      }
    }));
  }

  el("type-save").onclick = async () => {
    const name = el("type-name").value.trim();
    draft[promptKind] = textarea.value;
    const settings = {
      system_prompt: draft.system_prompt.trim(),
      output_prompt: draft.output_prompt.trim(),
      structure: draft.structure,
    };
    if (!name) {
      el("type-name").focus();
      return;
    }
    try {
      const saved = isNew
        ? await api.createType(name, settings)
        : await api.updateType(type.id, name, settings);
      toast(t("types.saved"));
      await reload(saved?.id ?? type?.id ?? null);
    } catch (error) {
      toast(error.message);
    }
  };

  if (isNew) el("type-name").focus();
}

// literal keys, so tests/test_pwa.py can verify them against the catalogs
function structureLabels() {
  return {
    paragraphs: t("types.structureParagraphs"),
    stanzas: t("types.structureStanzas"),
    dialogue: t("types.structureDialogue"),
    script: t("types.structureScript"),
  };
}

function showPrompt() {
  const textarea = el("type-prompt");
  if (!textarea) return;
  const isOutput = promptKind === "output_prompt";
  textarea.value = draft[promptKind];
  // an empty output prompt is valid — the default applies, so show it as the
  // placeholder instead of leaving the field looking broken
  textarea.placeholder = isOutput ? defaults.output_prompt : "";
  el("type-prompt-hint").textContent = isOutput
    ? t("types.promptOutputHint")
    : t("types.promptHint");
  el("type-prompt-default").hidden = !isOutput;
}

// Called by the router when another view takes over.
export function destroy() {
  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = null;
}
