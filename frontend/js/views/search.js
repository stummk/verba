// Semantic search: hybrid hit list over all transcripts, optional filters,
// and (with an LLM configured) a RAG answer whose sources jump into the
// editor at the exact audio position.
//
// A file appears exactly once, with all its hits below it in timeline order.
// Every hit is shown as a short excerpt with the query terms marked; the rest
// of the passage is elided, so the list stays scannable.

import { api } from "../api.js";
import { el, esc, formatDuration, html, raw, toast } from "../dom.js";
import { t } from "../i18n.js";
import { fillLanguageSelect } from "../languages.js";
import { renderMarkdown } from "../markdown.js";

// An excerpt is 1–3 lines: enough context around a match to recognise it,
// short enough that a file with many hits still fits on one screen.
const SNIPPET_MAX_CHARS = 240;
const SNIPPET_CONTEXT_CHARS = 55;
const SNIPPET_MAX_WINDOWS = 3;

export async function render(view) {
  const status = await api.searchStatus().catch(() => null);

  if (!status?.available) {
    view.replaceChildren(html`
      <h1>${t("search.title")}</h1>
      <div class="card">
        <p><strong>${t("search.unavailableTitle")}</strong></p>
        <p class="muted small">${t("search.unavailableText")}</p>
        <p><a class="btn tonal" href="#/setup">${t("dashboard.setupHintAction")}</a></p>
      </div>
    `);
    return;
  }

  const [projects, types] = await Promise.all([
    api.listProjects().catch(() => []),
    api.listTypes().catch(() => []),
  ]);

  view.replaceChildren(html`
    <h1>${t("search.title")}</h1>
    <div class="card">
      <form id="search-form" class="search-form">
        <input id="search-query" type="search" autocomplete="off"
               placeholder="${t("search.placeholder")}">
        <button type="submit">${t("search.go")}</button>
        <button type="button" class="tonal" id="search-ask" hidden>${t("search.ask")}</button>
      </form>
      <details class="advanced">
        <summary>${t("search.filters")}</summary>
        <div class="form-grid">
          <div>
            <label for="search-project">${t("search.filterProject")}</label>
            <select id="search-project">
              <option value="">${t("search.filterAll")}</option>
              ${raw(projects.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join(""))}
            </select>
          </div>
          <div>
            <label for="search-type">${t("search.filterType")}</label>
            <select id="search-type">
              <option value="">${t("search.filterAll")}</option>
              ${raw(types.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join(""))}
            </select>
          </div>
          <div>
            <label for="search-language">${t("search.filterLanguage")}</label>
            <select id="search-language"></select>
          </div>
          <div>
            <label for="search-speaker">${t("search.filterSpeaker")}</label>
            <input id="search-speaker" autocomplete="off">
          </div>
          <div>
            <label for="search-from">${t("search.filterFrom")}</label>
            <input id="search-from" type="date">
          </div>
          <div>
            <label for="search-to">${t("search.filterTo")}</label>
            <input id="search-to" type="date">
          </div>
        </div>
      </details>
      <p class="hint">${t("search.indexInfo", {
        files: status.files_indexed, chunks: status.chunk_count,
      })}</p>
    </div>
    <div id="search-answer"></div>
    <div id="search-results"></div>
  `);

  fillLanguageSelect(el("search-language"), { placeholder: t("search.filterAll") });

  const input = el("search-query");
  const askButton = el("search-ask");
  askButton.hidden = !status.llm_available;
  askButton.onclick = () => runAsk();
  askButton.disabled = true;

  // an emptied query means "nothing asked": the old hit list would otherwise
  // keep standing under an input that no longer says what it belongs to
  const onInput = () => {
    const query = input.value.trim();
    askButton.disabled = !query;
    if (!query) clearOutput();
  };
  input.addEventListener("input", onInput);
  input.addEventListener("search", onInput);

  el("search-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) {
      clearOutput();
      return;
    }
    el("search-answer").replaceChildren();
    const host = el("search-results");
    host.replaceChildren(html`<div class="card"><p class="muted small">…</p></div>`);
    try {
      const data = await api.search({ query, ...currentFilters() });
      renderResults(data.results, query);
    } catch (error) {
      host.replaceChildren();
      toast(error.message);
    }
  };

  function clearOutput() {
    el("search-answer").replaceChildren();
    el("search-results").replaceChildren();
  }

  function currentFilters() {
    return {
      project_id: el("search-project").value ? Number(el("search-project").value) : null,
      type_id: el("search-type").value ? Number(el("search-type").value) : null,
      language: el("search-language").value,
      speaker: el("search-speaker").value.trim(),
      date_from: el("search-from").value,
      date_to: el("search-to").value,
    };
  }

  function renderResults(results, query) {
    const host = el("search-results");
    if (!results?.length) {
      host.replaceChildren(html`
        <div class="card"><p class="muted">${t("search.noResults")}</p></div>
      `);
      return;
    }
    const terms = queryTerms(query);
    host.replaceChildren(...results.map((group) => fileCard(group, terms)));
  }

  // ── one card per file, its hits below it ────────────────────────────

  function fileCard(group, terms) {
    const card = document.createElement("div");
    card.className = "card search-file";

    const head = document.createElement("div");
    head.className = "search-file-head";
    head.append(
      Object.assign(document.createElement("span"), {
        className: "search-file-name",
        textContent: `${group.project_name} · ${group.title || group.filename}`,
      }),
    );
    // a header hit already carries date and header below — with its matches
    // marked — so the heading only repeats them when there is none
    const hasHeaderHit = group.hits.some((hit) => hit.source === "header");
    const details = hasHeaderHit
      ? ""
      : [group.recorded_at, group.header].filter(Boolean).join(" · ");
    if (details) {
      head.append(
        Object.assign(document.createElement("span"), {
          className: "small muted search-file-meta",
          textContent: details,
        }),
      );
    }
    head.append(
      Object.assign(document.createElement("span"), {
        className: "small muted search-file-count",
        textContent: t("search.hitCount", { count: group.hits.length }),
      }),
    );

    const list = document.createElement("ol");
    list.className = "search-hit-list";
    for (const hit of group.hits) {
      const item = document.createElement("li");
      item.append(hitLink(group, hit, terms));
      list.append(item);
    }
    card.append(head, list);
    return card;
  }

  function hitLink(group, hit, terms) {
    const link = document.createElement("a");
    link.className = "search-hit";
    link.href = `#/editor/${group.file_id}/${hit.start_s}`;

    const label = document.createElement("span");
    label.className = "search-hit-time";
    label.textContent =
      hit.source === "header" ? t("search.hitHeader") : formatDuration(hit.start_s);

    const text = document.createElement("p");
    text.className = "search-hit-text";
    appendSnippet(text, hit.text, terms);
    if (hit.speakers) {
      text.append(
        Object.assign(document.createElement("span"), {
          className: "small muted search-hit-speakers",
          textContent: ` — ${hit.speakers}`,
        }),
      );
    }
    link.append(label, text);
    return link;
  }

  // ── AI answer ──────────────────────────────────────────────────────

  async function runAsk() {
    const query = input.value.trim();
    if (!query) return;
    askButton.disabled = true;
    askButton.textContent = t("search.asking");
    try {
      const data = await api.searchAsk({ query, ...currentFilters() });
      renderResults(data.results, query);
      renderAnswer(data);
    } catch (error) {
      toast(error.message);
    } finally {
      askButton.disabled = !input.value.trim();
      askButton.textContent = t("search.ask");
    }
  }

  function renderAnswer(data) {
    const host = el("search-answer");
    if (!data.sources.length) {
      host.replaceChildren(html`
        <div class="card"><p class="muted">${t("search.noResults")}</p></div>
      `);
      return;
    }
    const card = document.createElement("div");
    card.className = "card";
    const heading = document.createElement("h2");
    heading.textContent = t("search.answerTitle");
    const answer = document.createElement("div");
    // markdown, but sanitised: model output never becomes raw markup
    answer.className = "docs-content search-answer-text";
    renderMarkdown(answer, data.answer);
    const sourcesTitle = document.createElement("p");
    sourcesTitle.className = "small muted";
    sourcesTitle.textContent = t("search.sources");
    const sources = document.createElement("ol");
    sources.className = "search-source-list";
    data.sources.forEach((source, index) => {
      const item = document.createElement("li");
      item.append(sourceLink(source, index + 1));
      sources.append(item);
    });
    card.append(heading, answer, sourcesTitle, sources);
    host.replaceChildren(card);
    host.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Sources are a reference list, not a second hit list: number, file and
  // position only — the passage itself stands in the answer above.
  function sourceLink(source, number) {
    const link = document.createElement("a");
    link.className = "search-source";
    link.href = `#/editor/${source.file_id}/${source.start_s}`;
    const position =
      source.source === "header" ? t("search.hitHeader") : formatDuration(source.start_s);
    link.textContent =
      `[${number}] ${source.project_name} · ` +
      `${source.title || source.filename} · ${position}`;
    return link;
  }
}

// ── excerpt with marked matches ──────────────────────────────────────

// The terms a hit is marked with: what the user typed, minus the noise that
// would highlight half the passage (single letters, but not the numbers of a
// date). Longest first, so "Katzen" wins over "Katze" where both match.
export function queryTerms(query) {
  const found = String(query ?? "")
    .toLowerCase()
    .match(/[\p{L}\p{N}]+/gu);
  const terms = [...new Set(found ?? [])].filter(
    (term) => term.length >= 3 || (/^\d+$/.test(term) && term.length >= 2),
  );
  return terms.sort((a, b) => b.length - a.length);
}

// All places a term occurs, merged where they overlap.
export function matchRanges(text, terms) {
  const haystack = text.toLowerCase();
  const found = [];
  for (const term of terms) {
    let from = 0;
    for (;;) {
      const at = haystack.indexOf(term, from);
      if (at < 0) break;
      found.push([at, at + term.length]);
      from = at + term.length;
    }
  }
  found.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const [start, end] of found) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

function snapStart(text, index) {
  if (index <= 0) return 0;
  const space = text.lastIndexOf(" ", index);
  return space < 0 ? index : space + 1;
}

function snapEnd(text, index) {
  if (index >= text.length) return text.length;
  const space = text.indexOf(" ", index);
  return space < 0 ? text.length : space;
}

// The stretches of the passage worth showing: a window of context around
// each match, up to the excerpt budget. Without a match (a purely semantic
// hit) the opening of the passage stands in for it.
export function snippetWindows(text, ranges) {
  if (!ranges.length) {
    return [{ from: 0, to: snapEnd(text, Math.min(text.length, SNIPPET_MAX_CHARS)) }];
  }
  const windows = [];
  let budget = SNIPPET_MAX_CHARS;
  for (const [start, end] of ranges) {
    const last = windows[windows.length - 1];
    if (last && start <= last.to) {
      const to = snapEnd(text, Math.min(text.length, end + SNIPPET_CONTEXT_CHARS));
      budget -= to - last.to;
      last.to = to;
    } else {
      if (windows.length >= SNIPPET_MAX_WINDOWS) break;
      const from = snapStart(text, Math.max(0, start - SNIPPET_CONTEXT_CHARS));
      const to = snapEnd(text, Math.min(text.length, end + SNIPPET_CONTEXT_CHARS));
      windows.push({ from, to });
      budget -= to - from;
    }
    if (budget <= 0) break;
  }
  return windows;
}

// Builds the excerpt as DOM nodes — the passage is user data and never
// becomes markup; <mark> elements carry the highlighting.
export function appendSnippet(host, text, terms) {
  const passage = String(text ?? "");
  const ranges = matchRanges(passage, terms);
  const windows = snippetWindows(passage, ranges);
  const ellipsis = (value) => host.append(document.createTextNode(value));

  windows.forEach((window, index) => {
    if (index > 0) ellipsis(" … ");
    else if (window.from > 0) ellipsis("… ");
    let cursor = window.from;
    for (const [start, end] of ranges) {
      if (end <= window.from) continue;
      if (start >= window.to) break;
      const from = Math.max(start, window.from);
      const to = Math.min(end, window.to);
      if (from > cursor) ellipsis(passage.slice(cursor, from));
      const mark = document.createElement("mark");
      mark.textContent = passage.slice(from, to);
      host.append(mark);
      cursor = to;
    }
    if (cursor < window.to) ellipsis(passage.slice(cursor, window.to));
  });
  const last = windows[windows.length - 1];
  if (last && last.to < passage.length) ellipsis(" …");
}
