// Project overview: list, create (via FAB dialog), open.

import { api } from "../api.js";
import { el, esc, html, raw, toast } from "../dom.js";
import { t } from "../i18n.js";
import { isActive } from "../jobs.js";
import { on } from "../ws.js";

let fabHandler = null;
let unsubscribers = [];

export async function render(view, systemStatus) {
  const [projects, types, jobs] = await Promise.all([
    api.listProjects(),
    api.listTypes(),
    api.listJobs(true).catch(() => []),
  ]);
  const ready = systemStatus?.ready ?? true;
  // one entry per transcript with work in flight, so the list already says
  // where something is running instead of making the user open every card
  const activeJobs = new Map(jobs.filter(isActive).map((job) => [job.id, job]));

  view.replaceChildren(html`
    <h1>${t("dashboard.title")}</h1>
    <div id="setup-hint"></div>
    <div id="project-list"></div>
    <dialog id="create-dialog">
      <h2>${t("dashboard.create")}</h2>
      <form id="create-project" method="dialog">
        <label for="project-name">${t("dashboard.newProjectName")}</label>
        <input id="project-name" maxlength="200" autocomplete="off"
               placeholder="${defaultProjectName()}">
        <label for="project-type">${t("dashboard.projectType")}</label>
        <select id="project-type">
          <option value="">${t("dashboard.noType")}</option>
          ${raw(types.map((type) => `<option value="${type.id}">${esc(type.name)}</option>`).join(""))}
        </select>
        <div class="actions">
          <button type="submit">${t("dashboard.create")}</button>
          <button type="button" class="text-btn" id="create-cancel">${t("common.cancel")}</button>
        </div>
      </form>
    </dialog>
  `);

  if (!ready) {
    el("setup-hint").replaceChildren(html`
      <div class="card">
        <p><strong>${t("dashboard.setupHintTitle")}</strong></p>
        <p class="muted small">${t("dashboard.setupHintText")}</p>
        <p><a class="btn tonal" href="#/setup">${t("dashboard.setupHintAction")}</a></p>
      </div>
    `);
  }

  renderList(projects, activeJobs);

  // live: a job that starts or finishes changes both the badge and the counts
  unsubscribers.forEach((off) => off());
  let refreshTimer = null;
  unsubscribers = [
    on("job.update", (job) => {
      if (isActive(job)) activeJobs.set(job.id, job);
      else activeJobs.delete(job.id);
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(async () => {
        if (!el("project-list")) return; // the user navigated away
        const fresh = await api.listProjects().catch(() => null);
        renderList(fresh ?? projects, activeJobs);
      }, 300);
    }),
  ];

  const dialog = el("create-dialog");
  el("create-cancel").onclick = () => dialog.close();
  el("create-project").onsubmit = async (event) => {
    event.preventDefault();
    // empty input falls back to the suggested default (today as yyyymmdd)
    const name = el("project-name").value.trim() || defaultProjectName();
    const typeId = el("project-type").value ? Number(el("project-type").value) : null;
    try {
      const created = await api.createProject(name, typeId);
      dialog.close();
      location.hash = `#/project/${created.id}`;
    } catch (error) {
      toast(t("dashboard.createError", { message: error.message }));
    }
  };

  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = () => {
    if (location.hash === "" || location.hash.startsWith("#/") === false || parseRoute() === "dashboard") {
      dialog.showModal();
      el("project-name").focus();
    }
  };
  window.addEventListener("fab:click", fabHandler);
}

function defaultProjectName() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}${month}${day}`;
}

function parseRoute() {
  return location.hash.replace(/^#\/?/, "").split("/")[0] || "dashboard";
}

function renderList(projects, activeJobs = new Map()) {
  const list = el("project-list");
  if (!list) return;
  if (!projects.length) {
    list.replaceChildren(html`<div class="card"><p class="muted">${t("dashboard.empty")}</p></div>`);
    return;
  }
  const running = new Map();
  for (const job of activeJobs.values()) {
    if (job.project_id == null) continue;
    running.set(job.project_id, (running.get(job.project_id) ?? 0) + 1);
  }
  list.replaceChildren(
    ...projects.map((project) => {
      const card = document.createElement("a");
      card.className = "card project-card";
      card.href = `#/project/${project.id}`;
      const title = document.createElement("strong");
      title.textContent = project.name;
      const count = running.get(project.id);
      if (count) {
        const badge = document.createElement("span");
        badge.className = "badge badge-running";
        badge.textContent = t("dashboard.running", { count });
        title.append(" ", badge);
      }
      card.append(
        title,
        Object.assign(document.createElement("span"), {
          className: "muted small",
          textContent: t("dashboard.fileStats", {
            count: project.file_count,
            done: project.done_count ?? 0,
          }),
        }),
      );
      return card;
    })
  );
}
