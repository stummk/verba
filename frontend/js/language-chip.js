// The one way a language is shown and changed in the UI: a chip carrying the
// flag, and a dialog with a filter behind it.
//
// A <select> holding a hundred languages is a poor thing to scroll on a phone
// and says nothing at a glance — the chip says which language a recording or a
// translation is in with a flag, names it in the tooltip, and opens a list that
// can be typed into instead of scrolled.

import { el, html } from "./dom.js";
import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";
import {
  flagsRenderable, languageFlag, languageLabel, languageName, languageRegion,
  sortedLanguages,
} from "./languages.js";

const HOST_ID = "lang-dialog-host";

/**
 * The flag of a language, in the best form the platform can show:
 *
 *  - the flag emoji, wherever the system draws them,
 *  - otherwise the two country letters in a flag-shaped box — Windows has no
 *    flag glyphs at all, and two stray indicator letters is what it would
 *    print instead, so they are put in a box on purpose,
 *  - the globe for a language that has no single country behind it.
 */
function flagNode(code) {
  const span = document.createElement("span");
  span.className = "lang-chip-flag";
  span.setAttribute("aria-hidden", "true");
  const region = code ? languageRegion(code) : "";
  if (!region) {
    span.classList.add("lang-chip-globe");
    span.innerHTML = iconSvg("language");
  } else if (flagsRenderable()) {
    span.textContent = languageFlag(code);
  } else {
    span.classList.add("lang-chip-region");
    span.textContent = region;
  }
  return span;
}

/**
 * Fill a chip button with a language — used on creation and on every change,
 * so a chip never has to be rebuilt to say something else.
 *
 * The chip itself stays short: the flag and the ISO code, or the word "auto"
 * where nothing is set. The full name is what the tooltip is for, together
 * with `hint` — what this particular language decides.
 *
 * @param {HTMLElement} chip
 * @param {string} code ISO code, "" for automatic detection
 * @param {{autoTitle?: string, hint?: string}} options
 */
export function setChipLanguage(chip, code, { autoTitle = t("picker.autoTitle"), hint = "" } = {}) {
  chip.dataset.code = code;
  chip.classList.toggle("auto", !code);
  const text = document.createElement("span");
  text.className = "lang-chip-text";
  text.textContent = code || t("picker.auto");
  chip.replaceChildren(...(code ? [flagNode(code), text] : [text]));
  const label = code ? languageLabel(code) : autoTitle;
  chip.title = hint ? `${label} — ${hint}` : label;
  chip.setAttribute("aria-label", chip.title);
}

/**
 * A language chip. Clicking it opens the picker; `onPick` receives the chosen
 * code ("" for automatic) and is not called when the dialog is cancelled.
 *
 * @param {{code?: string, autoTitle?: string, hint?: string, active?: boolean,
 *          dialogTitle?: string, auto?: boolean, groups?: Array,
 *          restLabel?: string, onPick?: (code: string) => void}} options
 * @returns {HTMLButtonElement}
 */
export function languageChip({
  code = "",
  autoTitle = t("picker.autoTitle"),
  hint = "",
  active = false,
  dialogTitle = t("picker.language"),
  auto = true,
  groups = null,
  restLabel = "",
  onPick = null,
} = {}) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "lang-chip";
  chip.classList.toggle("active", active);
  setChipLanguage(chip, code, { autoTitle, hint });
  chip.onclick = async () => {
    const chosen = await pickLanguage({
      title: dialogTitle,
      selected: chip.dataset.code,
      auto: auto ? autoTitle : null,
      groups,
      restLabel,
    });
    if (chosen === null) return;
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

/** Lowercase and stripped of accents, so "Turkce" finds "Türkçe". */
function foldCase(value) {
  return value.toLowerCase().normalize("NFD").replace(/\p{Diacritic}/gu, "");
}

function optionButton({ code, label, selected }) {
  const option = document.createElement("button");
  option.type = "button";
  option.className = "lang-option";
  option.dataset.code = code;
  option.dataset.search = foldCase(`${label} ${code}`);
  if (selected) option.classList.add("selected");
  option.append(flagNode(code), Object.assign(document.createElement("span"), {
    className: "lang-option-name", textContent: label,
  }));
  const codeNode = document.createElement("span");
  codeNode.className = "lang-option-code muted small";
  codeNode.textContent = code;
  option.append(codeNode);
  return option;
}

/**
 * Ask for a language. Resolves to the ISO code, "" for automatic detection,
 * or null when the dialog was cancelled — cancel, Escape and the backdrop all
 * answer null, so the caller leaves the current language alone.
 *
 * `groups` splits the list into named sections ([{label, codes}]); every
 * language not named in one lands under `restLabel`.
 *
 * @param {{title?: string, selected?: string, auto?: string | null,
 *          groups?: Array<{label: string, codes: string[]}> | null,
 *          restLabel?: string}} options
 * @returns {Promise<string | null>}
 */
export function pickLanguage({
  title = t("picker.language"),
  selected = "",
  auto = null,
  groups = null,
  restLabel = "",
} = {}) {
  const node = host();
  node.replaceChildren(html`
    <dialog id="lang-dialog" class="lang-dialog">
      <h2>${title}</h2>
      <input id="lang-filter" type="search" autocomplete="off"
             placeholder="${t("picker.filter")}" aria-label="${t("picker.filter")}">
      <div class="lang-list" id="lang-list"></div>
      <p class="muted small" id="lang-empty" hidden>${t("picker.noMatch")}</p>
      <div class="actions">
        <button type="button" class="text-btn" id="lang-cancel">${t("common.cancel")}</button>
      </div>
    </dialog>
  `);

  const dialog = el("lang-dialog");
  const list = el("lang-list");
  const grouped = new Set((groups ?? []).flatMap((group) => group.codes));

  if (auto !== null) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "lang-option lang-option-auto";
    option.dataset.code = "";
    option.dataset.search = foldCase(auto);
    if (!selected) option.classList.add("selected");
    option.textContent = auto;
    list.append(option);
  }
  for (const group of groups ?? []) {
    if (!group.codes.length) continue;
    list.append(sectionLabel(group.label));
    for (const code of group.codes) {
      list.append(optionButton({ code, label: languageName(code), selected: code === selected }));
    }
  }
  const rest = sortedLanguages().filter(({ code }) => !grouped.has(code));
  if (grouped.size && restLabel) list.append(sectionLabel(restLabel));
  for (const { code, name } of rest) {
    list.append(optionButton({ code, label: name, selected: code === selected }));
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
      const option = event.target.closest(".lang-option");
      if (option) finish(option.dataset.code);
    };
    el("lang-cancel").onclick = () => finish(null);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault(); // Escape: close it here, next to the answer
      finish(null);
    });
    dialog.addEventListener("close", () => finish(null));
    dialog.onclick = (event) => {
      if (event.target === dialog) finish(null); // the backdrop, not the box
    };

    // The filter is what makes the list usable at all: typing narrows it, and
    // Enter takes the first language still standing — no scrolling needed.
    const filter = el("lang-filter");
    const apply = () => {
      const needle = foldCase(filter.value.trim());
      let visible = 0;
      for (const option of list.querySelectorAll(".lang-option")) {
        const match = !needle || option.dataset.search.includes(needle);
        option.hidden = !match;
        if (match) visible += 1;
      }
      // a section whose languages are all filtered away keeps no heading
      for (const label of list.querySelectorAll(".lang-section")) {
        let sibling = label.nextElementSibling;
        let any = false;
        while (sibling && !sibling.classList.contains("lang-section")) {
          if (!sibling.hidden) any = true;
          sibling = sibling.nextElementSibling;
        }
        label.hidden = !any;
      }
      el("lang-empty").hidden = visible > 0;
    };
    filter.oninput = apply;
    filter.onkeydown = (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const first = [...list.querySelectorAll(".lang-option")].find((o) => !o.hidden);
      if (first) finish(first.dataset.code);
    };

    dialog.showModal();
    list.querySelector(".lang-option.selected")?.scrollIntoView({ block: "center" });
    filter.focus();
  });
}

function sectionLabel(text) {
  const label = document.createElement("p");
  label.className = "lang-section muted small";
  label.textContent = text;
  return label;
}
