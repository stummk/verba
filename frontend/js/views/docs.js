// In-app documentation: the user guide as collapsible sections with a quick
// navigation — several sections can stay open at the same time.

import { marked } from "/vendor/marked.esm.js";
import { api } from "../api.js";
import { el, html } from "../dom.js";
import { currentLanguage, t } from "../i18n.js";

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
    <div class="docs-content docs-intro" id="docs-intro"></div>
    <nav class="docs-toc" id="docs-toc" aria-label="${t("docs.toc")}"></nav>
    <div class="docs-actions">
      <button type="button" class="text-btn small-btn" id="docs-expand">
        ${t("docs.expandAll")}
      </button>
      <button type="button" class="text-btn small-btn" id="docs-collapse">
        ${t("docs.collapseAll")}
      </button>
    </div>
    <div id="docs-sections"></div>
  `);

  // trusted content: the guide ships with the application itself
  el("docs-intro").innerHTML = marked.parse(intro);

  const host = el("docs-sections");
  const entries = sections.map(({ title, body }) => {
    const details = document.createElement("details");
    details.className = "card docs-section";
    const summary = document.createElement("summary");
    summary.textContent = title;
    const content = document.createElement("div");
    content.className = "docs-content";
    content.innerHTML = marked.parse(body);
    details.append(summary, content);
    host.append(details);
    return details;
  });

  const toc = el("docs-toc");
  entries.forEach((details, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = sections[index].title;
    button.onclick = () => {
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    toc.append(button);
  });

  if (entries.length) entries[0].open = true;
  el("docs-expand").onclick = () => entries.forEach((d) => (d.open = true));
  el("docs-collapse").onclick = () => entries.forEach((d) => (d.open = false));
}

// Split the markdown into the intro (before the first "## ") and one section
// per "## " heading; fenced code blocks are ignored while scanning.
function splitSections(markdown) {
  const introLines = [];
  const sections = [];
  let current = null;
  let inFence = false;
  for (const line of markdown.split("\n")) {
    if (line.trimStart().startsWith("```")) inFence = !inFence;
    if (!inFence && line.startsWith("## ")) {
      current = { title: line.slice(3).trim(), lines: [] };
      sections.push(current);
      continue;
    }
    (current ? current.lines : introLines).push(line);
  }
  return {
    intro: introLines.join("\n").trim(),
    sections: sections.map((s) => ({ title: s.title, body: s.lines.join("\n").trim() })),
  };
}
