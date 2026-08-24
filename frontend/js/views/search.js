// Semantic search: hybrid hit list over all transcripts, optional filters,
// and (with an LLM configured) a RAG answer whose sources jump into the
// editor at the exact audio position.

import { api } from "../api.js";
import { el, esc, formatDuration, html, raw, toast } from "../dom.js";
import { t } from "../i18n.js";
import { fillLanguageSelect } from "../languages.js";

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

  let lastQuery = "";

  el("search-form").onsubmit = async (event) => {
    event.preventDefault();
    const query = el("search-query").value.trim();
    if (!query) return;
    lastQuery = query;
    el("search-answer").replaceChildren();
    const host = el("search-results");
    host.replaceChildren(html`<div class="card"><p class="muted small">…</p></div>`);
    try {
      const data = await api.search({ query, ...currentFilters() });
      renderResults(data);
    } catch (error) {
      host.replaceChildren();
      toast(error.message);
    }
  };

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

  function renderResults({ results, llm_available: llmAvailable }) {
    const host = el("search-results");
    if (!results.length) {
      host.replaceChildren(html`
        <div class="card"><p class="muted">${t("search.noResults")}</p></div>
      `);
      return;
    }
    const cards = results.map((hit) => resultCard(hit));
    if (llmAvailable) {
      const bar = document.createElement("div");
      bar.className = "actions";
      const askButton = document.createElement("button");
      askButton.type = "button";
      askButton.className = "tonal";
      askButton.textContent = t("search.ask");
      askButton.onclick = () => runAsk(askButton);
      bar.append(askButton);
      host.replaceChildren(bar, ...cards);
    } else {
      host.replaceChildren(...cards);
    }
  }

  function resultCard(hit, number = null) {
    const card = document.createElement("a");
    card.className = "card search-hit";
    card.href = `#/editor/${hit.file_id}/${hit.start_s}`;
    const meta = document.createElement("div");
    meta.className = "small muted search-hit-meta";
    const prefix = number !== null ? `[${number}] ` : "";
    const speakers = hit.speakers ? ` · ${hit.speakers}` : "";
    meta.textContent =
      `${prefix}${hit.project_name} · ${hit.title || hit.filename} · ` +
      `${formatDuration(hit.start_s)}${speakers}`;
    const text = document.createElement("p");
    text.className = "search-hit-text";
    text.textContent = hit.text;
    card.append(meta, text);
    return card;
  }

  async function runAsk(button) {
    button.disabled = true;
    button.textContent = t("search.asking");
    try {
      const data = await api.searchAsk({ query: lastQuery, ...currentFilters() });
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
      const answer = document.createElement("p");
      answer.className = "search-answer-text";
      answer.textContent = data.answer;
      const sourcesTitle = document.createElement("p");
      sourcesTitle.className = "small muted";
      sourcesTitle.textContent = t("search.sources");
      card.append(heading, answer, sourcesTitle);
      data.sources.forEach((source, index) => card.append(resultCard(source, index + 1)));
      host.replaceChildren(card);
      host.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      button.textContent = t("search.ask");
    }
  }
}
