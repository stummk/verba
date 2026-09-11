// The hit list of the semantic search and the RAG answer above it.
//
// A file appears exactly once, with all its hits below it in timeline order.
// Every hit is shown as a short excerpt with the query terms marked; the rest
// of the passage is elided, so the list stays scannable. A source in the AI
// answer jumps into the editor at the exact audio position.
//
// The search lives in the transcript overview (views/dashboard.js), which is
// where these two render into — the results take the place of the project
// cards for as long as something was asked.

import { formatDuration } from "./dom.js";
import { t } from "./i18n.js";
import { renderMarkdown } from "./markdown.js";

// An excerpt is 1–3 lines: enough context around a match to recognise it,
// short enough that a file with many hits still fits on one screen.
const SNIPPET_MAX_CHARS = 240;
const SNIPPET_CONTEXT_CHARS = 55;
const SNIPPET_MAX_WINDOWS = 3;

export function renderResults(host, results, query) {
  if (!results?.length) {
    const card = document.createElement("div");
    card.className = "card";
    card.append(
      Object.assign(document.createElement("p"), {
        className: "muted",
        textContent: t("search.noResults"),
      }),
    );
    host.replaceChildren(card);
    return;
  }
  const terms = queryTerms(query);
  host.replaceChildren(...results.map((group) => fileCard(group, terms)));
}

// ── one card per file, its hits below it ──────────────────────────────

// The heading is the header the user wrote — its three fields, the empty
// ones left out. Only where none of them is filled does the file name stand
// in for it. Deliberately not the title: that one is parsed out of the file
// name at import and never written again, so it shows what an older parser
// made of the name long after the header has been corrected.
function fileCard(group, terms) {
  const card = document.createElement("div");
  card.className = "card search-file";

  const head = document.createElement("div");
  head.className = "search-file-head";
  const name = document.createElement("span");
  name.className = "search-file-name";
  appendSnippet(name, group.label || group.filename, terms);
  head.append(
    name,
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

  // Where the hits come from, quietly at the foot of the card: the recording
  // itself. Skipped when the heading already is the file name.
  if (group.filename && group.filename !== (group.label || group.filename)) {
    const source = document.createElement("a");
    source.className = "small muted search-file-source";
    source.href = `#/editor/${group.file_id}`;
    source.textContent = group.filename;
    source.title = t("search.openFile");
    card.append(source);
  }
  return card;
}

// Where a hit sits: an audio position for the transcript, the panel it lives
// in for a derived text (those are one flowing text and have no timestamps).
function hitTarget(group, hit) {
  if (hit.source === "cleanup") return `#/editor/${group.file_id}/0/cleanup`;
  if (hit.source === "translation") {
    return `#/editor/${group.file_id}/0/translation-${hit.source_language || ""}`;
  }
  return `#/editor/${group.file_id}/${hit.start_s}`;
}

function hitLabel(hit) {
  if (hit.source === "header") return t("search.hitHeader");
  if (hit.source === "cleanup") return t("search.hitCleanup");
  if (hit.source === "translation") {
    return t("search.hitTranslation", { language: (hit.source_language || "").toUpperCase() });
  }
  return formatDuration(hit.start_s);
}

function hitLink(group, hit, terms) {
  const link = document.createElement("a");
  link.className = "search-hit";
  link.href = hitTarget(group, hit);

  const label = document.createElement("span");
  label.className = "search-hit-time";
  label.textContent = hitLabel(hit);

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

// ── AI answer ────────────────────────────────────────────────────────

export function renderAnswer(host, data) {
  if (!data.sources.length) {
    const empty = document.createElement("div");
    empty.className = "card";
    empty.append(
      Object.assign(document.createElement("p"), {
        className: "muted",
        textContent: t("search.noResults"),
      }),
    );
    host.replaceChildren(empty);
    return;
  }
  const card = document.createElement("div");
  card.className = "card search-answer";
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
}

// Sources are a reference list, not a second hit list: number, file and
// position only — the passage itself stands in the answer above.
function sourceLink(source, number) {
  const link = document.createElement("a");
  link.className = "search-source";
  link.href = hitTarget({ file_id: source.file_id }, source);
  link.textContent =
    `[${number}] ${source.project_name} · ${fileLabel(source)} · ${hitLabel(source)}`;
  return link;
}

// What a transcript is called: its header fields, the empty ones left out,
// and the file name where none of them is filled in. The grouped hit list
// gets this from the backend as `label`; a raw source hit carries the fields
// themselves, so the RAG reference list computes it here.
export function fileLabel(row) {
  const header = [row.header_left, row.header_middle, row.header_right]
    .map((part) => String(part ?? "").trim())
    .filter(Boolean)
    .join(" · ");
  return header || String(row.filename ?? "").trim();
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
