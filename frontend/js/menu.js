// The overflow menu of a card: one three-dot button, and the actions behind it
// as icon + label. A card in a list has room for one or two controls, not for
// six — and an icon-only row of six says nothing about what any of them does.
//
// The menu is anchored in the card itself (absolutely positioned, no
// coordinate arithmetic) and flips above the button when there is no room
// below it. Only one menu is ever open: opening a second closes the first.
//
// The right mouse button asks for the same list, and asks for it where the
// pointer stands — `contextMenu()` is therefore the same sheet with the same
// items, only placed at a coordinate instead of under a button. A card that
// has a three-dot menu and a waveform that has none both go through it.

import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";

let openMenu = null; // the only open menu, so a click elsewhere can close it

function closeOpenMenu() {
  if (!openMenu) return;
  const { wrap, sheet, button } = openMenu;
  openMenu = null;
  // a sheet opened at the pointer belongs to nothing — it exists for as long
  // as it is open and is thrown away with the close
  if (!wrap) {
    sheet.remove();
    return;
  }
  sheet.hidden = true;
  // the open menu is lifted out of the cards below it; closed, it must not
  // keep sitting on top of them
  wrap.classList.remove("open");
  button.classList.remove("active");
  button.setAttribute("aria-expanded", "false");
}

/** Whether a menu is on screen — Escape belongs to it before anything else. */
export function menuIsOpen() {
  return openMenu !== null;
}

// A click anywhere else, Escape, or the page scrolling away under the menu:
// all three mean the same thing — this menu is no longer what is being used.
document.addEventListener("pointerdown", (event) => {
  if (openMenu && !event.target.closest(".card-menu, .menu-sheet")) closeOpenMenu();
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && openMenu) {
    const { button } = openMenu;
    closeOpenMenu();
    button?.focus();   // a menu opened at the pointer has none to go back to
  }
});
window.addEventListener("scroll", closeOpenMenu, true);

/**
 * An overflow menu.
 *
 * `items` is built on every open, not once — a card's actions depend on what
 * the file has behind it, and that changes while the menu sits in the DOM.
 * An item is `{icon, label, onSelect, danger?}`; a falsy entry is skipped, so
 * a caller can write conditions inline.
 *
 * @param {{label?: string, items: () => Array<object|null|false>}} options
 * @returns {HTMLElement} the wrapper — place it where the button belongs
 */
export function overflowMenu({ label = t("menu.more"), items }) {
  const wrap = document.createElement("div");
  wrap.className = "card-menu";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-btn";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = iconSvg("moreVert");

  const sheet = document.createElement("div");
  sheet.className = "menu-sheet";
  sheet.setAttribute("role", "menu");
  sheet.hidden = true;

  button.onclick = (event) => {
    // the card around it is a link to the editor, and opening a menu is not a
    // request to leave the list
    event.preventDefault();
    event.stopPropagation();
    if (openMenu?.sheet === sheet) {
      closeOpenMenu();
      return;
    }
    closeOpenMenu();
    sheet.replaceChildren(...items().filter(Boolean).map(menuItem));
    if (!sheet.childElementCount) return;
    sheet.hidden = false;
    wrap.classList.add("open");
    button.classList.add("active");
    button.setAttribute("aria-expanded", "true");
    openMenu = { wrap, sheet, button };
    // no room below (the last card of a long list): open upwards instead
    sheet.classList.toggle("up", sheet.getBoundingClientRect().bottom > window.innerHeight);
    sheet.querySelector(".menu-item")?.focus();
  };

  // a click on the sheet's padding must not reach the card behind it either
  sheet.onclick = swallow;
  wrap.append(button, sheet);
  return wrap;
}

/**
 * The same actions at the pointer instead of under a button — the right mouse
 * button on a file card, or on the waveform, which has no button to hang them
 * on. Nothing is opened when the list is empty: a menu with no entries is a
 * rectangle that says the right button is broken.
 *
 * @param {{x: number, y: number, items: Array<object|null|false>}} options
 */
export function contextMenu({ x, y, items }) {
  closeOpenMenu();
  const entries = items.filter(Boolean);
  if (!entries.length) return;
  const sheet = document.createElement("div");
  sheet.className = "menu-sheet floating";
  sheet.setAttribute("role", "menu");
  sheet.replaceChildren(...entries.map(menuItem));
  // a click on the sheet's padding must not reach whatever is behind it
  sheet.onclick = swallow;
  document.body.append(sheet);
  placeAt(sheet, x, y);
  openMenu = { sheet };
  sheet.querySelector(".menu-item")?.focus();
}

/**
 * The right mouse button on `host` opens `items()` where the pointer stands.
 * Over a text field the browser's own menu is the better answer — cut, paste
 * and the spell checker's suggestions are things this menu does not have — so
 * `skip` leaves it alone there.
 */
export function bindContextMenu(host, items, { skip = "input, textarea, select" } = {}) {
  host.addEventListener("contextmenu", (event) => {
    if (skip && event.target.closest(skip)) return;
    event.preventDefault();
    contextMenu({ x: event.clientX, y: event.clientY, items: items() });
  });
}

// The pointer position comes in pixels — it is the one number the browser only
// hands over that way — and is written back in rem, the unit the rest of the
// styling is in. The sheet is measured after it is in the document, so a menu
// near the right or the bottom edge moves inside instead of being cut off.
function placeAt(sheet, x, y) {
  const unit = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const margin = 0.5 * unit;
  const box = sheet.getBoundingClientRect();
  const left = Math.max(margin, Math.min(x, window.innerWidth - box.width - margin));
  const top = Math.max(margin, Math.min(y, window.innerHeight - box.height - margin));
  sheet.style.left = `${left / unit}rem`;
  sheet.style.top = `${top / unit}rem`;
}

function menuItem({ icon, label: itemLabel, onSelect, danger = false }) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = danger ? "menu-item danger-item" : "menu-item";
  item.setAttribute("role", "menuitem");
  item.innerHTML = iconSvg(icon);
  item.append(Object.assign(document.createElement("span"), { textContent: itemLabel }));
  item.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    closeOpenMenu();
    onSelect();
  };
  return item;
}

function swallow(event) {
  event.preventDefault();
  event.stopPropagation();
}
