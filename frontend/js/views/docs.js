// In-app documentation: the user guide as collapsible sections, each headed by
// an icon, plus a question box that lets the configured LLM answer from the
// guide itself. One question, one answer — every question starts a fresh
// context, so nothing carries over from the previous one.

import { marked } from "/vendor/marked.esm.js";
import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { iconSvg } from "../icons.js";
import { currentLanguage, t } from "../i18n.js";
import { renderMarkdown } from "../markdown.js";

// The "{#slug}" marker on every "## " heading in docs/user/*.md is the same in
// all three languages, so one icon map covers them all.
const SECTION_ICONS = {
  install: "download",
  "first-run": "checklist",
  transcripts: "folder",
  types: "category",
  import: "upload",
  transcribe: "mic",
  ai: "sparkle",
  editor: "edit",
  whisper: "memory",
  llm: "cloud",
  pdf: "pdf",
  search: "search",
  settings: "tune",
  security: "lock",
  api: "key",
};

export async function render(view) {
  let docs;
  try {
    docs = await api.getDocs(currentLanguage());
  } catch (error) {
    view.replaceChildren(html`<div class="card">${t("docs.missing")} (${error.message})</div>`);
    return;
  }

  const { intro, sections } = splitSections(docs.content);

  view.replaceChildren(html`
    <p><a href="#/settings" class="muted small">${t("docs.back")}</a></p>
    <div class="card docs-ask" id="docs-ask" hidden>
      <h2>${t("docs.askTitle")}</h2>
      <p class="muted small">${t("docs.askIntro")}</p>
      <textarea id="docs-question" rows="2" maxlength="1000"
                placeholder="${t("docs.askPlaceholder")}"></textarea>
      <div class="actions">
        <button type="button" id="docs-ask-run">${t("docs.askRun")}</button>
      </div>
      <div id="docs-answer" hidden>
        <h3 class="subhead">${t("docs.answerTitle")}</h3>
        <div class="docs-content docs-answer-text" id="docs-answer-text"></div>
        <p class="hint" id="docs-answer-sources"></p>
      </div>
    </div>
    <div class="docs-content docs-intro" id="docs-intro"></div>
    <div id="docs-sections"></div>
  `);

  // trusted content: the guide ships with the application itself
  el("docs-intro").innerHTML = marked.parse(intro);

  const host = el("docs-sections");
  const entries = sections.map(({ title, slug, body }) => {
    const details = document.createElement("details");
    details.className = "card docs-section";
    const summary = document.createElement("summary");
    const icon = document.createElement("span");
    icon.className = "docs-section-icon";
    icon.innerHTML = iconSvg(SECTION_ICONS[slug] ?? "article");
    summary.append(icon, Object.assign(document.createElement("span"), { textContent: title }));
    const content = document.createElement("div");
    content.className = "docs-content";
    content.innerHTML = marked.parse(body);
    details.append(summary, content);
    host.append(details);
    return details;
  });

  if (entries.length) entries[0].open = true;

  // no LLM configured → no question box at all, rather than a dead button
  if (docs.llm_available) bindAsk();
}

function bindAsk() {
  const card = el("docs-ask");
  const button = el("docs-ask-run");
  const question = el("docs-question");
  const answer = el("docs-answer");
  const text = el("docs-answer-text");
  const sources = el("docs-answer-sources");
  card.hidden = false;

  const ask = async () => {
    const value = question.value.trim();
    if (value.length < 3) return;
    button.disabled = true;
    answer.hidden = false;
    text.textContent = t("docs.asking");
    sources.textContent = "";
    try {
      const result = await api.docsAsk(value, currentLanguage());
      // markdown, but sanitised: model output never becomes raw markup
      if (result.answer) renderMarkdown(text, result.answer);
      else text.textContent = t("docs.askEmpty");
      const titles = (result.sections ?? []).map((section) => section.title).join(", ");
      const used = titles ? t("docs.answerSources", { sections: titles }) : "";
      sources.textContent = result.truncated ? `${used} ${t("docs.answerFiltered")}`.trim() : used;
    } catch (error) {
      answer.hidden = true;
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  };

  button.onclick = ask;
  question.onkeydown = (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) ask();
  };
}

// Split the markdown into the intro (before the first "## ") and one section
// per "## " heading; fenced code blocks are ignored while scanning. A heading
// may carry a "{#slug}" marker, which selects the icon and never shows up in
// the rendered title.
function splitSections(markdown) {
  const introLines = [];
  const sections = [];
  let current = null;
  let inFence = false;
  for (const line of markdown.split("\n")) {
    if (line.trimStart().startsWith("```")) inFence = !inFence;
    if (!inFence && line.startsWith("## ")) {
      const heading = line.slice(3).trim();
      const marker = heading.match(/\s*\{#([a-z0-9-]+)\}$/);
      current = {
        title: marker ? heading.slice(0, marker.index).trim() : heading,
        slug: marker ? marker[1] : "",
        lines: [],
      };
      sections.push(current);
      continue;
    }
    (current ? current.lines : introLines).push(line);
  }
  return {
    intro: introLines.join("\n").trim(),
    sections: sections.map((s) => ({
      title: s.title,
      slug: s.slug,
      body: s.lines.join("\n").trim(),
    })),
  };
}
