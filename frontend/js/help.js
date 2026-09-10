// The explanation behind a "?" — there when it is asked for, not before.
//
// Every field used to carry its description underneath as running text, which
// turned a settings card into three screens of prose with the fields lost in
// it. The texts are unchanged; they moved into a bubble that a hover, a focus
// or — on a touch screen, where there is no hover — a tap opens.
//
// One bubble serves the whole app, positioned at the button it belongs to: a
// card that scrolls or clips its content can never cut an explanation in half.
// What stays visible next to a field is only what changes — a path, a status,
// a verdict about this machine.

import { esc } from "./dom.js";
import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";

/**
 * The "?" that carries a description.
 *
 * Returns HTML, like `iconSvg()` — inside an `html` template it goes through
 * `raw()`. The text travels in the attribute, so no view has to keep it.
 *
 * @param {string} text the explanation, plain text
 * @returns {string} the button markup, or "" for an empty text
 */
export function helpIcon(text) {
  if (!text) return "";
  return (
    `<button type="button" class="help-btn" data-help="${esc(text)}"` +
    ` aria-label="${esc(t("common.help"))}" aria-expanded="false">` +
    `${iconSvg("help")}</button>`
  );
}

/**
 * A "?" whose text the view fills in later.
 *
 * An explanation is known when the markup is written; what a row has to say
 * about itself — the state of an update, of the GPU libraries — is not. So
 * the button is rendered empty and hidden, and `setHelp()` gives it its text
 * as soon as the backend has answered. Nothing to say means no "?" at all.
 *
 * @param {string} id id of the button, for `setHelp()`
 * @returns {string} the button markup
 */
export function helpSlot(id) {
  return (
    `<button type="button" class="help-btn" id="${esc(id)}" hidden` +
    ` aria-label="${esc(t("common.help"))}" aria-expanded="false">` +
    `${iconSvg("help")}</button>`
  );
}

/**
 * Put a text behind a "?" that was rendered by `helpSlot()`.
 *
 * An open bubble is rewritten in place: the text under the pointer must be
 * the one that is true now, not the one that was true when it opened.
 *
 * @param {HTMLElement|null} button the slot, or nothing (the view moved on)
 * @param {string} text the explanation, plain text — empty hides the "?"
 */
export function setHelp(button, text) {
  if (!button) return;
  button.dataset.help = text ?? "";
  button.hidden = !button.dataset.help;
  if (current !== button) return;
  if (button.hidden) hide();
  else {
    bubble.textContent = button.dataset.help;
    place(button);
  }
}

/**
 * Several explanations about one field — one bubble, a blank line apart.
 *
 * @param {...string} parts the texts, empty ones are dropped
 * @returns {string} the explanation
 */
export function helpText(...parts) {
  return parts.filter(Boolean).join("\n\n");
}

/**
 * A label with its explanation behind it.
 *
 * The button is a sibling of the label, never a child: a `<label>` may not
 * contain interactive content other than its own control, and a click inside
 * one would otherwise land on the field. The row takes over the spacing the
 * label brought with it, which is why it has to be told what it holds.
 *
 * @param {string} innerHtml the complete label (or heading) element
 * @param {string} text the explanation, plain text
 * @param {string} rowClass "field-row", "check-row" or "head-row"
 * @returns {string} the row markup
 */
export function labelHelp(innerHtml, text, rowClass = "field-row") {
  if (!text) return innerHtml;
  return `<div class="label-row ${rowClass}">${innerHtml}${helpIcon(text)}</div>`;
}

/**
 * The label of a form field, with its explanation behind it.
 *
 * @param {string} forId id of the field the label belongs to
 * @param {string} text the label
 * @param {string} help the explanation, plain text
 * @returns {string} the row markup
 */
export function fieldLabel(forId, text, help) {
  return labelHelp(`<label for="${esc(forId)}">${esc(text)}</label>`, help);
}

/**
 * A checkbox with its label and its explanation behind it.
 *
 * @param {string} id id of the checkbox
 * @param {string} text the label
 * @param {string} help the explanation, plain text
 * @returns {string} the row markup
 */
export function checkLine(id, text, help) {
  const label =
    `<label class="checkline"><input type="checkbox" id="${esc(id)}"> ${esc(text)}</label>`;
  return labelHelp(label, help, "check-row");
}

// ── the one bubble ───────────────────────────────────────────────────

const BUBBLE_ID = "help-bubble";
// How long the bubble survives the pointer leaving the "?" — just enough to
// reach the bubble itself, which a long text has to allow.
const LEAVE_GRACE = 140;

let bubble = null;
let current = null; // the "?" whose text is showing
let pinned = false; // opened by click or tap: it stays until it is dismissed
let hideTimer = null;

function ensureBubble() {
  // a dialog that rebuilt its markup took the bubble with it
  if (bubble?.isConnected) return bubble;
  bubble = document.createElement("div");
  bubble.className = "help-bubble";
  bubble.id = BUBBLE_ID;
  bubble.setAttribute("role", "tooltip");
  bubble.hidden = true;
  bubble.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  bubble.addEventListener("pointerleave", () => {
    if (!pinned) hide();
  });
  document.body.appendChild(bubble);
  return bubble;
}

// A modal <dialog> renders in the top layer, above everything the page can
// reach with a z-index — so inside one the bubble has to live in the dialog
// itself. It stays `position: fixed` either way: the dialog carries no
// transform, so the viewport remains the containing block.
function hostFor(button) {
  return button.closest("dialog") ?? document.body;
}

// The bubble is placed in rem, like everything else here: the numbers come
// from measured rectangles, and dividing them by the root font size keeps the
// styling in the unit the rest of the app is written in.
function rootUnit() {
  return parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
}

function place(button) {
  const unit = rootUnit();
  const gap = 0.4 * unit; // between the "?" and the bubble
  const margin = 0.6 * unit; // never closer to the window edge than this
  // measure at the natural size the stylesheet gives it
  bubble.style.left = "0";
  bubble.style.top = "0";
  const anchor = button.getBoundingClientRect();
  const box = bubble.getBoundingClientRect();
  const maxLeft = window.innerWidth - box.width - margin;
  const left = Math.max(margin, Math.min(anchor.left + anchor.width / 2 - box.width / 2, maxLeft));
  let top = anchor.bottom + gap;
  if (top + box.height > window.innerHeight - margin) {
    const above = anchor.top - gap - box.height;
    top = above >= margin ? above : Math.max(margin, window.innerHeight - box.height - margin);
  }
  bubble.style.left = `${left / unit}rem`;
  bubble.style.top = `${top / unit}rem`;
}

function show(button, pin = false) {
  const text = button.dataset.help;
  if (!text) return;
  clearTimeout(hideTimer);
  const node = ensureBubble();
  if (current && current !== button) release(current);
  current = button;
  pinned = pin;
  const host = hostFor(button);
  if (node.parentElement !== host) host.appendChild(node);
  node.textContent = text;
  node.hidden = false;
  button.setAttribute("aria-expanded", "true");
  button.setAttribute("aria-describedby", BUBBLE_ID);
  place(button);
}

function release(button) {
  button.setAttribute("aria-expanded", "false");
  button.removeAttribute("aria-describedby");
}

function hide() {
  clearTimeout(hideTimer);
  if (bubble) bubble.hidden = true;
  if (current) release(current);
  current = null;
  pinned = false;
}

function trigger(event) {
  const target = event.target;
  return target instanceof Element ? target.closest(".help-btn") : null;
}

let installed = false;

function install() {
  if (installed) return;
  installed = true;

  document.addEventListener("pointerover", (event) => {
    // a finger has no hover — its tap arrives as a click, which pins the text
    if (event.pointerType === "touch") return;
    const button = trigger(event);
    if (button && button !== current) show(button);
  });
  document.addEventListener("pointerout", (event) => {
    const button = trigger(event);
    if (!button || button !== current || pinned) return;
    hideTimer = setTimeout(hide, LEAVE_GRACE);
  });
  // capture: the "?" may sit inside a row that listens for clicks of its own
  document.addEventListener("click", (event) => {
    const button = trigger(event);
    if (!button) {
      hide();
      return;
    }
    // the "?" sits next to a label and inside a form: neither may react
    event.preventDefault();
    event.stopPropagation();
    if (pinned && current === button) hide();
    else show(button, true);
  }, true);
  document.addEventListener("focusin", (event) => {
    const button = trigger(event);
    if (button) show(button);
    else if (!pinned) hide();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !current) return;
    // an open explanation is what Escape closes — a dialog behind it stays
    event.preventDefault();
    event.stopPropagation();
    hide();
  }, true);
  window.addEventListener("resize", hide);
  // the anchor moves out from under the bubble as soon as anything scrolls
  window.addEventListener("scroll", hide, true);
}

install();
