// Transcript type management: master-detail — list (selection) plus edit pane.
// Mobile shows one pane at a time; the FAB creates a new type.

import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { iconButton } from "../icons.js";
import { t } from "../i18n.js";

let fabHandler = null;
let types = [];
let selected = null; // a type object, "new", or null

export async function render(view) {
  types = await api.listTypes();
  selected = null;

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

  renderList();
  renderDetail();
}

function currentRoute() {
  return location.hash.replace(/^#\/?/, "").split("/")[0] || "dashboard";
}

async function reload(keepId = null) {
  types = await api.listTypes();
  selected = keepId === null ? null : (types.find((type) => type.id === keepId) ?? null);
  renderList();
  renderDetail();
}

function select(target) {
  selected = target;
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
    <label for="type-prompt">${t("types.prompt")}</label>
    <textarea id="type-prompt" class="type-prompt" maxlength="8000"></textarea>
    <p class="hint">${t("types.promptHint")}</p>
    <div class="actions">
      <button type="button" id="type-save">${t("common.save")}</button>
    </div>
  `);

  el("type-name").value = type?.name ?? "";
  el("type-prompt").value = type?.system_prompt ?? "";
  el("types-back").onclick = () => select(null);

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
    const prompt = el("type-prompt").value.trim();
    if (!name) {
      el("type-name").focus();
      return;
    }
    try {
      const saved = isNew
        ? await api.createType(name, prompt)
        : await api.updateType(type.id, name, prompt);
      toast(t("types.saved"));
      await reload(saved?.id ?? type?.id ?? null);
    } catch (error) {
      toast(error.message);
    }
  };

  if (isNew) el("type-name").focus();
}
