// The search header of the transcript overview.
//
// One bar at the top of the page, fixed there while the list scrolls under
// it: the query field, the magnifier that searches every transcript and the
// icon next to it that has the LLM answer the question instead — and below
// both the row of tag filters (`search-filters.js`).
//
// Searching does not open a page of its own. As long as a query is standing,
// the hit list takes the place of the project cards; clearing the field — by
// hand or with the × — puts them back. The filters keep working either way:
// they narrow the cards and the hit list by exactly the same rule.
//
// The state lives in the module, not in the view, so coming back from a
// transcript or from the editor finds the search as it was left.

import { api } from "./api.js";
import { el, esc, html, toast } from "./dom.js";
import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";
import { asQuery, currentFilters, isEmpty, mountFilters } from "./search-filters.js";
import { renderAnswer, renderResults } from "./search-results.js";

// What the user last asked, and how — kept across navigations so returning
// from a hit does not throw the result list away.
let query = "";
let mode = ""; // "" (nothing asked), "search" or "ask"
let status = null;
let onProjectFilters = () => {};
let onModeChange = () => {};

export function searchIsActive() {
  return Boolean(mode && query);
}

/** The filter set as the project list wants it. */
export function projectFilterQuery() {
  return asQuery();
}

export function filtersAreEmpty() {
  return isEmpty(currentFilters());
}

// The markup of the header, as an escaped HTML string: the view drops it in
// with raw() at the top of its own template.
export function headerMarkup() {
  return `
    <div class="search-header" id="search-header">
      <form class="search-bar" id="search-form" role="search">
        <button type="submit" class="search-bar-btn" id="search-run"></button>
        <button type="button" class="search-bar-btn" id="search-ask" hidden></button>
        <input id="search-query" type="search" autocomplete="off" enterkeyhint="search"
               placeholder="${esc(t("search.placeholder"))}"
               aria-label="${esc(t("search.title"))}">
        <button type="button" class="search-bar-btn" id="search-clear" hidden></button>
      </form>
      <div class="filter-chips" id="filter-chips"></div>
    </div>
    <dialog id="filter-dialog" class="filter-dialog">
      <h2 id="filter-dialog-title"></h2>
      <form id="filter-dialog-form" method="dialog">
        <div id="filter-dialog-body"></div>
        <div class="actions">
          <button type="submit">${esc(t("common.ok"))}</button>
          <button type="button" class="text-btn" id="filter-dialog-cancel">
            ${esc(t("common.cancel"))}
          </button>
          <span class="spacer"></span>
          <button type="button" class="text-btn" id="filter-dialog-reset" hidden>
            ${esc(t("filters.clear"))}
          </button>
        </div>
      </form>
    </dialog>
  `;
}

/**
 * Wire the header up.
 *
 * @param {HTMLElement} output   where the hit list and the answer are rendered
 * @param {Function} filtersChanged  called when the tag filters changed
 * @param {Function} modeChanged called when the page switches between the
 *                               project cards and a search result
 */
export async function mountSearchHeader(output, filtersChanged, modeChanged) {
  onProjectFilters = filtersChanged;
  onModeChange = modeChanged;

  const input = el("search-query");
  const runButton = el("search-run");
  const askButton = el("search-ask");
  const clearButton = el("search-clear");

  decorate(runButton, "search", t("search.go"));
  decorate(askButton, "sparkle", t("search.ask"));
  decorate(clearButton, "close", t("search.clear"));

  input.value = query;
  clearButton.hidden = !query;

  el("search-form").onsubmit = (event) => {
    event.preventDefault();
    run("search");
  };
  askButton.onclick = () => run("ask");
  clearButton.onclick = () => {
    input.value = "";
    input.focus();
    onInput();
  };
  input.addEventListener("input", onInput);
  // the native × of a search field also fires this, nothing else does
  input.addEventListener("search", onInput);
  // Enter searches. Explicitly, rather than leaving it to the form's implicit
  // submission: that one depends on the browser finding the submit button, and
  // the search key of a phone keyboard is not worth that bet.
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    run("search");
  });

  function onInput() {
    query = input.value.trim();
    clearButton.hidden = !query;
    // an emptied field means "nothing asked": the old hit list would otherwise
    // keep standing under an input that no longer says what it belongs to
    if (!query && mode) {
      mode = "";
      output.replaceChildren();
      onModeChange();
    }
  }

  await mountFilters(el("filter-chips"), el("filter-dialog"), () => {
    onProjectFilters();
    if (searchIsActive()) run(mode); // the filters narrow the hit list as well
  });

  // The index is what the query field needs, not the filters — so the chips
  // are usable even where the search components were never installed.
  status = await api.searchStatus().catch(() => null);
  if (!el("search-query")) return; // the view was replaced while we asked
  askButton.hidden = !status?.llm_available;
  if (!status?.available) {
    input.disabled = true;
    runButton.disabled = true;
    askButton.hidden = true;
    input.placeholder = t("search.unavailableShort");
    el("search-form").title = t("search.unavailableTitle");
    return;
  }
  input.title = t("search.indexInfo", {
    files: status.files_indexed,
    chunks: status.chunk_count,
  });
  if (searchIsActive()) run(mode); // a result that was standing comes back

  async function run(which) {
    query = input.value.trim();
    if (!query) {
      onInput();
      return;
    }
    const wasActive = mode !== "";
    mode = which;
    if (!wasActive) onModeChange(); // the project cards make room first
    output.replaceChildren(html`<div class="card"><p class="muted small">…</p></div>`);
    const busy = which === "ask" ? askButton : runButton;
    busy.disabled = true;
    busy.classList.add("busy");
    try {
      const payload = { query, ...asQuery() };
      if (which === "ask") {
        const data = await api.searchAsk(payload);
        if (!el("search-query")) return;
        const answer = document.createElement("div");
        const hits = document.createElement("div");
        output.replaceChildren(answer, hits);
        renderAnswer(answer, data);
        renderResults(hits, data.results, query);
      } else {
        const data = await api.search(payload);
        if (!el("search-query")) return;
        renderResults(output, data.results, query);
      }
    } catch (error) {
      output.replaceChildren();
      mode = "";
      onModeChange();
      toast(error.message);
    } finally {
      busy.disabled = false;
      busy.classList.remove("busy");
    }
  }
}

function decorate(button, icon, label) {
  button.innerHTML = iconSvg(icon);
  button.title = label;
  button.setAttribute("aria-label", label);
}
