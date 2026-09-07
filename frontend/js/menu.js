// The overflow menu of a card: one three-dot button, and the actions behind it
// as icon + label. A card in a list has room for one or two controls, not for
// six — and an icon-only row of six says nothing about what any of them does.
//
// The menu is anchored in the card itself (absolutely positioned, no
// coordinate arithmetic) and flips above the button when there is no room
// below it. Only one menu is ever open: opening a second closes the first.

import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";

let openMenu = null; // the only open menu, so a click elsewhere can close it

function closeOpenMenu() {
  if (!openMenu) return;
  const { wrap, sheet, button } = openMenu;
  openMenu = null;
  sheet.hidden = true;
  // the open menu is lifted out of the cards below it; closed, it must not
  // keep sitting on top of them
  wrap.classList.remove("open");
  button.classList.remove("active");
  button.setAttribute("aria-expanded", "false");
}

// A click anywhere else, Escape, or the page scrolling away under the menu:
// all three mean the same thing — this menu is no longer what is being used.
document.addEventListener("pointerdown", (event) => {
  if (openMenu && !event.target.closest(".card-menu")) closeOpenMenu();
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && openMenu) {
    const { button } = openMenu;
    closeOpenMenu();
    button.focus();
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

  // a click on the sheet's padding must not reach the card behind it either
  sheet.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
  };

  wrap.append(button, sheet);
  return wrap;
}
