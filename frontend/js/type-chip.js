// The one way a transcript type is shown and changed on a file: a chip
// carrying the name, and a picker behind it.
//
// A project's type is the rule for everything in it, but a project holds what
// was recorded, and that is not always of one kind — a rehearsal with the
// songs next to the conversation about them. So a file may name a type of its
// own, and the chip has to say which of the two is speaking: the project's
// type is drawn as inherited, the file's own as set.

import { el, html } from "./dom.js";
import { t } from "./i18n.js";

const HOST_ID = "type-dialog-host";

/**
 * Fill a chip with the type that applies to a file — on creation and on every
 * change, so the chip never has to be rebuilt to say something else.
 *
 * @param {HTMLElement} chip
 * @param {number|null} typeId the file's own type, null while it follows its project
 * @param {{types: Array<{id: number, name: string}>, projectTypeName?: string}} options
 */
export function setChipType(chip, typeId, { types = [], projectTypeName = "" } = {}) {
  const own = typeId ? types.find((entry) => entry.id === typeId) : null;
  chip.dataset.typeId = own ? String(own.id) : "";
  chip.classList.toggle("inherited", !own);
  const name = own ? own.name : projectTypeName;
  // the name lives in a span of its own: the chip is a flex button, and
  // `text-overflow` needs a block box to cut a long type name short
  chip.replaceChildren(Object.assign(document.createElement("span"), {
    className: "file-type-chip-text", textContent: name || t("type.none"),
  }));
  chip.title = own
    ? t("type.ownTitle", { name: own.name })
    : t("type.inheritedTitle", { name: projectTypeName || t("type.none") });
  chip.setAttribute("aria-label", chip.title);
}

/**
 * A transcript-type chip for one file. Clicking it opens the picker; `onPick`
 * receives the chosen type id (null for "follow the project") and is not
 * called when the dialog is cancelled.
 *
 * @param {{typeId?: number|null, types?: Array, projectTypeName?: string,
 *          onPick?: (typeId: number|null) => void}} options
 * @returns {HTMLButtonElement}
 */
export function typeChip({ typeId = null, types = [], projectTypeName = "", onPick = null } = {}) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "file-type-chip";
  setChipType(chip, typeId, { types, projectTypeName });
  chip.onclick = async () => {
    const chosen = await pickType({
      selected: chip.dataset.typeId ? Number(chip.dataset.typeId) : null,
      types,
      projectTypeName,
    });
    if (chosen === undefined) return; // cancelled — null is a real answer
    if (onPick) onPick(chosen);
  };
  return chip;
}

function host() {
  let node = el(HOST_ID);
  if (!node) {
    node = document.createElement("div");
    node.id = HOST_ID;
    document.body.appendChild(node);
  }
  return node;
}

function optionButton({ id, label, hint, selected }) {
  const option = document.createElement("button");
  option.type = "button";
  option.className = "type-option";
  option.dataset.typeId = id === null ? "" : String(id);
  if (selected) option.classList.add("selected");
  option.append(Object.assign(document.createElement("span"), {
    className: "type-option-name", textContent: label,
  }));
  if (hint) {
    option.append(Object.assign(document.createElement("span"), {
      className: "type-option-hint muted small", textContent: hint,
    }));
  }
  return option;
}

/**
 * Ask which transcript type a file should follow. Resolves to the type id,
 * null for "whatever the project says", or undefined when the dialog was
 * cancelled — null is an answer of its own here, so cancelling cannot use it.
 *
 * @param {{selected?: number|null, types?: Array, projectTypeName?: string}} options
 * @returns {Promise<number | null | undefined>}
 */
export function pickType({ selected = null, types = [], projectTypeName = "" } = {}) {
  const node = host();
  node.replaceChildren(html`
    <dialog id="type-dialog" class="type-dialog">
      <h2>${t("type.pick")}</h2>
      <div class="type-list" id="type-list"></div>
      <div class="actions">
        <button type="button" class="text-btn" id="type-cancel">${t("common.cancel")}</button>
      </div>
    </dialog>
  `);

  const dialog = el("type-dialog");
  const list = el("type-list");
  list.append(optionButton({
    id: null,
    label: t("type.fromProject"),
    hint: projectTypeName || t("type.none"),
    selected: selected === null,
  }));
  for (const entry of types) {
    list.append(optionButton({
      id: entry.id,
      label: entry.name,
      hint: entry.builtin ? t("types.builtin") : "",
      selected: entry.id === selected,
    }));
  }

  return new Promise((resolve) => {
    let settled = false;
    function finish(answer) {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      node.replaceChildren();
      resolve(answer);
    }
    list.onclick = (event) => {
      const option = event.target.closest(".type-option");
      if (option) finish(option.dataset.typeId ? Number(option.dataset.typeId) : null);
    };
    el("type-cancel").onclick = () => finish(undefined);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault(); // Escape: close it here, next to the answer
      finish(undefined);
    });
    dialog.addEventListener("close", () => finish(undefined));
    dialog.onclick = (event) => {
      if (event.target === dialog) finish(undefined); // the backdrop, not the box
    };

    dialog.showModal();
    list.querySelector(".type-option.selected")?.scrollIntoView({ block: "center" });
  });
}
