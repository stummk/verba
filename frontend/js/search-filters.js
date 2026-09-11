// The tag filters of the search header.
//
// One chip per field — transcript type, date, language, speaker, state — in a
// row that scrolls sideways when it no longer fits. A chip opens a dialog in
// which several values are ticked at once; confirming it narrows the
// transcript overview and, while one is running, the search at the same time.
// An active chip carries what it stands for and an × that clears it alone.
//
// The values on offer are never free text: the backend answers with what
// actually occurs in the material this user may see (`/api/projects/filters`),
// so a picker cannot offer a language nobody ever recorded in.

import { api } from "./api.js";
import { dragScroll, el, html } from "./dom.js";
import { iconSvg } from "./icons.js";
import { currentLanguage, t } from "./i18n.js";
import { languageName } from "./languages.js";

// The empty filter set — also what "clear everything" resets to.
export function emptyFilters() {
  return { type_ids: [], languages: [], speakers: [], statuses: [], date_from: "", date_to: "" };
}

export function isEmpty(filters) {
  return (
    !filters.type_ids.length &&
    !filters.languages.length &&
    !filters.speakers.length &&
    !filters.statuses.length &&
    !filters.date_from &&
    !filters.date_to
  );
}

// The state of every chip in one object — the same shape the search request
// and the project list both take, so nothing has to be translated on the way.
let filters = emptyFilters();
let options = null;
let onChange = () => {};
let dialog = null;
let chipRow = null;

export function currentFilters() {
  return { ...filters, type_ids: [...filters.type_ids] };
}

/** The values the chips currently stand for, as the API takes them. */
export function asQuery() {
  return {
    type_ids: filters.type_ids,
    languages: filters.languages,
    speakers: filters.speakers,
    statuses: filters.statuses,
    date_from: filters.date_from,
    date_to: filters.date_to,
  };
}

// ── the chips ────────────────────────────────────────────────────────

// A date range is one chip but two values, so it is described rather than
// listed like the others. Everything else is a set of ticks.
const DEFINITIONS = [
  {
    key: "type_ids",
    icon: "category",
    label: () => t("filters.type"),
    values: () => (options?.types ?? []).map((row) => ({ value: row.id, label: row.name })),
  },
  { key: "date", icon: "dateRange", label: () => t("filters.date"), kind: "date" },
  {
    key: "languages",
    // the icon the translation wears everywhere else in the app: a language
    // is a language, whether it was spoken or translated into
    icon: "translate",
    label: () => t("filters.language"),
    values: () =>
      (options?.languages ?? []).map((row) => ({
        value: row.code,
        label: languageName(row.code) || row.code.toUpperCase(),
        count: row.count,
      })),
  },
  {
    key: "speakers",
    icon: "people",
    label: () => t("filters.speaker"),
    values: () =>
      (options?.speakers ?? []).map((row) => ({
        value: row.name,
        label: row.name,
        count: row.count,
      })),
  },
  {
    key: "statuses",
    icon: "checklist",
    label: () => t("filters.status"),
    values: () =>
      (options?.statuses ?? []).map((row) => ({
        value: row.status,
        label: t(`status.${row.status}`) || row.status,
        count: row.count,
      })),
  },
];

// The chip says the range the way the date fields below it do — the ISO form
// belongs to the API, not onto a tag that has to fit in one line.
function showDate(value) {
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(currentLanguage(), {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

function dateSummary() {
  const from = filters.date_from;
  const to = filters.date_to;
  if (from && to) {
    return from === to ? showDate(from) : `${showDate(from)} – ${showDate(to)}`;
  }
  if (from) return t("filters.dateFromShort", { date: showDate(from) });
  if (to) return t("filters.dateToShort", { date: showDate(to) });
  return "";
}

// What an active chip says instead of its field name: the one value it was
// given, or how many there are — a chip is a tag, not a list.
function summary(definition) {
  if (definition.kind === "date") return dateSummary();
  const chosen = filters[definition.key];
  if (!chosen.length) return "";
  if (chosen.length === 1) {
    const match = definition.values().find((entry) => String(entry.value) === String(chosen[0]));
    return match?.label ?? String(chosen[0]);
  }
  return t("filters.chosen", { count: chosen.length });
}

function isSet(definition) {
  return definition.kind === "date"
    ? Boolean(filters.date_from || filters.date_to)
    : filters[definition.key].length > 0;
}

function clear(definition) {
  if (definition.kind === "date") {
    filters.date_from = "";
    filters.date_to = "";
  } else {
    filters[definition.key] = [];
  }
}

function chip(definition) {
  const active = isSet(definition);
  const wrapper = document.createElement("span");
  wrapper.className = `filter-chip${active ? " active" : ""}`;

  const main = document.createElement("button");
  main.type = "button";
  main.className = "filter-chip-main";
  main.innerHTML = iconSvg(definition.icon);
  const text = document.createElement("span");
  text.className = "filter-chip-label";
  text.textContent = active ? summary(definition) : definition.label();
  main.append(text);
  main.title = active ? `${definition.label()}: ${summary(definition)}` : definition.label();
  main.onclick = () => openDialog(definition);
  wrapper.append(main);

  if (active) {
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "filter-chip-clear";
    reset.innerHTML = iconSvg("close");
    reset.title = t("filters.clearOne", { name: definition.label() });
    reset.setAttribute("aria-label", reset.title);
    reset.onclick = () => {
      clear(definition);
      renderChips();
      onChange();
    };
    wrapper.append(reset);
  }
  return wrapper;
}

function renderChips() {
  if (!chipRow) return;
  const nodes = DEFINITIONS.map(chip);
  if (!isEmpty(filters)) {
    const all = document.createElement("button");
    all.type = "button";
    all.className = "filter-chip-reset";
    all.textContent = t("filters.clearAll");
    all.onclick = () => {
      filters = emptyFilters();
      renderChips();
      onChange();
    };
    nodes.push(all);
  }
  chipRow.replaceChildren(...nodes);
}

// ── the dialog behind a chip ─────────────────────────────────────────

function optionRow(entry, chosen) {
  const row = document.createElement("label");
  row.className = "filter-option";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "row-select";
  box.value = String(entry.value);
  box.checked = chosen.has(String(entry.value));
  const label = document.createElement("span");
  label.className = "filter-option-label";
  label.textContent = entry.label;
  row.append(box, label);
  if (entry.count != null) {
    row.append(
      Object.assign(document.createElement("span"), {
        className: "small muted filter-option-count",
        textContent: String(entry.count),
      }),
    );
  }
  return row;
}

// ── the date range picker ────────────────────────────────────────────
//
// A calendar, not two date fields: a range is two days *and* what lies
// between them, and that is a thing to see rather than to type. The first
// click opens the range, the second closes it; while only the start stands,
// the day under the pointer stands in for the end, so the range is visible
// before it is committed.

// The range while the dialog is open — confirming writes it into the
// filters, cancelling throws it away.
let draftFrom = "";
let draftTo = "";
let shownMonth = null; // first day of the month on screen
let hoverDay = ""; // the day the pointer is on, for the preview

function isoDay(date) {
  // never toISOString(): that one is UTC and moves the day across midnight
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function fromIso(value) {
  return value ? new Date(`${value}T00:00:00`) : null;
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

// Quick answers for the date range — what one actually asks a recording
// archive: today, the last week, the last month, this year.
function datePresets() {
  const today = new Date();
  const back = (days) => {
    const date = new Date(today);
    date.setDate(date.getDate() - days);
    return isoDay(date);
  };
  return [
    { label: t("filters.dateToday"), from: isoDay(today), to: isoDay(today) },
    { label: t("filters.dateWeek"), from: back(6), to: isoDay(today) },
    { label: t("filters.dateMonth"), from: back(29), to: isoDay(today) },
    { label: t("filters.dateYear"), from: `${today.getFullYear()}-01-01`, to: isoDay(today) },
  ];
}

// Which day a week starts on here: Monday in German and Russian, Sunday in
// English. The browser knows; a locale it has no answer for gets Monday.
function weekStart() {
  try {
    const locale = new Intl.Locale(currentLanguage());
    const info = locale.weekInfo ?? locale.getWeekInfo?.();
    if (info?.firstDay) return info.firstDay % 7; // 7 (Sunday) becomes 0
  } catch {
    /* the fallback below is the answer */
  }
  return 1;
}

function weekdayNames() {
  const formatter = new Intl.DateTimeFormat(currentLanguage(), { weekday: "short" });
  const first = weekStart();
  // 2024-01-07 was a Sunday, so the weekday index is the offset itself
  return Array.from({ length: 7 }, (unused, index) =>
    formatter.format(new Date(2024, 0, 7 + ((first + index) % 7))),
  );
}

/** A click on a day: the first one opens a range, the second one closes it. */
function pickDay(iso) {
  if (!draftFrom || draftTo || iso < draftFrom) {
    draftFrom = iso;
    draftTo = "";
  } else {
    draftTo = iso;
  }
  hoverDay = "";
  renderCalendar();
}

// While only the start is set, the day under the pointer stands in for the end.
function previewEnd() {
  if (draftFrom && !draftTo && hoverDay > draftFrom) return hoverDay;
  return draftTo;
}

function monthButton(icon, label, step) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "daterange-nav";
  button.innerHTML = iconSvg(icon);
  button.title = label;
  button.setAttribute("aria-label", label);
  button.onclick = () => {
    shownMonth = new Date(shownMonth.getFullYear(), shownMonth.getMonth() + step, 1);
    renderCalendar();
  };
  return button;
}

function dayButton(date, iso, inMonth, todayIso) {
  const end = previewEnd();
  const button = document.createElement("button");
  button.type = "button";
  button.className = "daterange-day";
  button.textContent = String(date.getDate());
  button.dataset.day = iso;
  button.setAttribute("aria-label", showDate(iso));
  if (!inMonth) button.classList.add("other");
  if (iso === todayIso) button.classList.add("today");
  const spans = Boolean(draftFrom && end && end !== draftFrom);
  if (iso === draftFrom || (end && iso === end)) {
    button.classList.add("edge");
    if (spans) button.classList.add(iso === draftFrom ? "start" : "end");
  } else if (spans && iso > draftFrom && iso < end) {
    button.classList.add("inside");
  }
  button.onclick = () => pickDay(iso);
  return button;
}

function renderCalendar() {
  const host = el("filter-calendar");
  if (!host) return;

  const head = document.createElement("div");
  head.className = "daterange-head";
  head.append(
    monthButton("chevronLeft", t("filters.monthBack"), -1),
    Object.assign(document.createElement("span"), {
      className: "daterange-title",
      textContent: shownMonth.toLocaleDateString(currentLanguage(), {
        month: "long",
        year: "numeric",
      }),
    }),
    monthButton("chevronRight", t("filters.monthForward"), 1),
  );

  const grid = document.createElement("div");
  grid.className = "daterange-grid";
  for (const name of weekdayNames()) {
    grid.append(
      Object.assign(document.createElement("span"), {
        className: "daterange-weekday",
        textContent: name,
      }),
    );
  }
  // always six weeks, so the dialog keeps its height from month to month
  const lead = (shownMonth.getDay() - weekStart() + 7) % 7;
  const todayIso = isoDay(new Date());
  for (let index = 0; index < 42; index += 1) {
    const date = new Date(shownMonth.getFullYear(), shownMonth.getMonth(), 1 - lead + index);
    grid.append(dayButton(date, isoDay(date), date.getMonth() === shownMonth.getMonth(), todayIso));
  }
  // the preview follows the pointer — one listener on the grid, not 42
  grid.onpointerover = (event) => {
    const day = event.target.closest(".daterange-day")?.dataset.day;
    if (!day || day === hoverDay || !draftFrom || draftTo) return;
    hoverDay = day;
    renderCalendar();
  };
  grid.onpointerleave = () => {
    if (!hoverDay) return;
    hoverDay = "";
    renderCalendar();
  };

  host.replaceChildren(
    head,
    grid,
    Object.assign(document.createElement("p"), {
      className: "daterange-summary",
      textContent: draftFrom || draftTo ? draftSummary() : t("filters.datePickHint"),
    }),
  );
}

function draftSummary() {
  if (draftFrom && draftTo) {
    return draftFrom === draftTo
      ? showDate(draftFrom)
      : `${showDate(draftFrom)} – ${showDate(draftTo)}`;
  }
  if (draftFrom) return t("filters.dateFromShort", { date: showDate(draftFrom) });
  return t("filters.dateToShort", { date: showDate(draftTo) });
}

function buildDateBody(body) {
  draftFrom = filters.date_from;
  draftTo = filters.date_to;
  hoverDay = "";
  // open where the range is, otherwise where the newest recording is
  shownMonth = startOfMonth(fromIso(draftFrom) ?? fromIso(options?.date_last) ?? new Date());

  body.replaceChildren(html`
    <div class="filter-date-presets" id="filter-presets"></div>
    <div class="daterange" id="filter-calendar"></div>
    <p class="hint" id="filter-date-range"></p>
  `);
  el("filter-presets").replaceChildren(
    ...datePresets().map((preset) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn tonal small-btn";
      button.textContent = preset.label;
      button.onclick = () => {
        draftFrom = preset.from;
        draftTo = preset.to;
        shownMonth = startOfMonth(fromIso(preset.to));
        renderCalendar();
      };
      return button;
    }),
  );
  renderCalendar();
  // what there is to pick from at all, so an empty range is not a mystery
  if (options?.date_first) {
    el("filter-date-range").textContent = t("filters.dateAvailable", {
      from: showDate(options.date_first),
      to: showDate(options.date_last),
    });
  }
}

function buildListBody(body, definition) {
  const entries = definition.values();
  const chosen = new Set(filters[definition.key].map(String));
  if (!entries.length) {
    body.replaceChildren(html`<p class="muted">${t("filters.noOptions")}</p>`);
    return;
  }
  body.replaceChildren(html`
    <input id="filter-search" type="search" autocomplete="off" hidden
           placeholder="${t("filters.search")}">
    <div class="filter-options" id="filter-options"></div>
  `);
  const list = el("filter-options");
  list.replaceChildren(...entries.map((entry) => optionRow(entry, chosen)));

  // a handful of types needs no search field; three hundred speakers do
  const search = el("filter-search");
  search.hidden = entries.length < 8;
  search.oninput = () => {
    const needle = search.value.trim().toLowerCase();
    for (const row of list.children) {
      row.hidden = Boolean(needle) && !row.textContent.toLowerCase().includes(needle);
    }
  };
}

function openDialog(definition) {
  const body = el("filter-dialog-body");
  el("filter-dialog-title").textContent = definition.label();
  if (definition.kind === "date") buildDateBody(body);
  else buildListBody(body, definition);

  const reset = el("filter-dialog-reset");
  reset.hidden = !isSet(definition);
  reset.onclick = () => {
    clear(definition);
    dialog.close();
    renderChips();
    onChange();
  };
  el("filter-dialog-form").onsubmit = (event) => {
    event.preventDefault();
    if (definition.kind === "date") {
      // one day clicked and nothing after it means exactly that day: the
      // calendar has no second click left to wait for once the dialog is shut
      filters.date_from = draftFrom;
      filters.date_to = draftTo || draftFrom;
    } else {
      filters[definition.key] = [...el("filter-options").querySelectorAll("input:checked")].map(
        (box) => (definition.key === "type_ids" ? Number(box.value) : box.value),
      );
    }
    dialog.close();
    renderChips();
    onChange();
  };
  dialog.showModal();
}

// ── mounting ─────────────────────────────────────────────────────────

/**
 * Build the chip row into `host` and keep it in step with the filter state.
 *
 * @param {HTMLElement} host    the row the chips live in
 * @param {HTMLElement} dialogNode the shared dialog (markup from the view)
 * @param {Function} changed    called whenever the filters changed
 */
export async function mountFilters(host, dialogNode, changed) {
  chipRow = dragScroll(host);
  dialog = dialogNode;
  onChange = changed;
  el("filter-dialog-cancel").onclick = () => dialog.close();
  renderChips(); // the row stands before the options arrive
  options = await api.projectFilters().catch(() => null);
  if (!chipRow.isConnected) return; // the view was replaced while we asked
  renderChips();
}

/** Forget the chips — the next visit starts without a filter. */
export function resetFilters() {
  filters = emptyFilters();
}
