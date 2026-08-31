// Project overview: list, create (via FAB dialog), open, rename, delete.

import { api } from "../api.js";
import { el, esc, html, raw, toast } from "../dom.js";
import { iconButton } from "../icons.js";
import { t } from "../i18n.js";
import { isActive } from "../jobs.js";
import { on } from "../ws.js";

let fabHandler = null;
let unsubscribers = [];
let refreshTimer = null;
let currentProjectId = null;
let renameDialog = null;
let deleteDialog = null;
let renameInput = null;

export async function render(view, systemStatus) {
  const [projects, types, jobs] = await Promise.all([
    api.listProjects(),
    api.listTypes(),
    api.listJobs(true).catch(() => []),
  ]);
  const ready = systemStatus?.ready ?? true;
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
    <dialog id="rename-dialog">
      <h2>${t("dashboard.renameTitle")}</h2>
      <form id="rename-project" method="dialog">
        <label for="rename-name">${t("dashboard.renameLabel")}</label>
        <input id="rename-name" maxlength="200" autocomplete="off">
        <div class="actions">
          <button type="submit">${t("dashboard.rename")}</button>
          <button type="button" class="text-btn" id="rename-cancel">${t("common.cancel")}</button>
        </div>
      </form>
    </dialog>
    <dialog id="delete-dialog">
      <h2>${t("dashboard.deleteTitle")}</h2>
      <p id="delete-message"></p>
      <div class="actions">
        <button type="button" class="tonal" id="delete-confirm">${t("common.delete")}</button>
        <button type="button" class="text-btn" id="delete-cancel">${t("common.cancel")}</button>
      </div>
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

  renameDialog = el("rename-dialog");
  renameInput = el("rename-name");
  deleteDialog = el("delete-dialog");

  renderList(projects, activeJobs);

  unsubscribers.forEach((off) => off());
  unsubscribers = [
    on("job.update", (job) => {
      if (isActive(job)) activeJobs.set(job.id, job);
      else activeJobs.delete(job.id);
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(async () => {
        if (!el("project-list")) return;
        await refreshProjects();
      }, 300);
    }),
  ];

  const createDialog = el("create-dialog");
  el("create-cancel").onclick = () => createDialog.close();
  el("create-project").onsubmit = async (event) => {
    event.preventDefault();
    const name = el("project-name").value.trim() || defaultProjectName();
    const typeId = el("project-type").value ? Number(el("project-type").value) : null;
    try {
      const created = await api.createProject(name, typeId);
      createDialog.close();
      location.hash = `#/project/${created.id}`;
    } catch (error) {
      toast(t("dashboard.createError", { message: error.message }));
    }
  };

  el("rename-cancel").onclick = () => renameDialog?.close();
  el("rename-project").onsubmit = async (event) => {
    event.preventDefault();
    if (!currentProjectId) return;
    const name = renameInput.value.trim();
    if (!name) return;
    try {
      await api.updateProject(currentProjectId, { name });
      toast(t("dashboard.renameSuccess"));
      renameDialog.close();
      await refreshProjects();
    } catch (error) {
      toast(t("dashboard.renameError", { message: error.message }));
    }
  };

  el("delete-cancel").onclick = () => deleteDialog?.close();
  el("delete-confirm").onclick = async () => {
    if (!currentProjectId) return;
    try {
      await api.deleteProject(currentProjectId);
      toast(t("dashboard.deleted"));
      deleteDialog.close();
      await refreshProjects();
    } catch (error) {
      toast(t("dashboard.deleteError", { message: error.message }));
    }
  };

  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = () => {
    if (location.hash === "" || location.hash.startsWith("#/") === false || parseRoute() === "dashboard") {
      createDialog.showModal();
      el("project-name").focus();
    }
  };
  window.addEventListener("fab:click", fabHandler);
}

async function refreshProjects() {
  const fresh = await api.listProjects().catch(() => null);
  if (!fresh) return;
  const jobs = await api.listJobs(true).catch(() => []);
  const activeJobs = new Map(jobs.filter(isActive).map((job) => [job.id, job]));
  renderList(fresh, activeJobs);
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
      card.addEventListener("click", (event) => {
        if (event.target.closest("button")) event.preventDefault();
      });
      const header = document.createElement("div");
      header.className = "project-card-header";
      const title = document.createElement("strong");
      title.textContent = project.name;
      const count = running.get(project.id);
      if (count) {
        const badge = document.createElement("span");
        badge.className = "badge badge-running";
        badge.textContent = t("dashboard.running", { count });
        title.append(" ", badge);
      }
      const left = document.createElement("div");
      left.className = "project-card-left";
      left.append(title);
      const right = document.createElement("div");
      right.className = "project-card-right";
      if (project.type_name) {
        const typeBadge = document.createElement("span");
        typeBadge.className = "badge badge-type";
        typeBadge.textContent = project.type_name;
        right.append(typeBadge);
      }
      const actions = document.createElement("div");
      actions.className = "project-card-actions";
      const renameBtn = iconButton("edit", t("dashboard.rename"), (event) => {
        event.preventDefault();
        event.stopPropagation();
        currentProjectId = project.id;
        renameInput.value = project.name;
        renameDialog.showModal();
        renameInput.focus();
      });
      const deleteBtn = iconButton("delete", t("common.delete"), (event) => {
        event.preventDefault();
        event.stopPropagation();
        currentProjectId = project.id;
        el("delete-message").textContent = t("dashboard.deleteConfirm", { name: project.name });
        deleteDialog.showModal();
      });
      actions.append(renameBtn, deleteBtn);
      right.append(actions);
      header.append(left, right);
      card.append(
        header,
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

// Called by the router when another view takes over: nothing of this one may
// keep running in the background.
export function destroy() {
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  clearTimeout(refreshTimer);
  if (fabHandler) window.removeEventListener("fab:click", fabHandler);
  fabHandler = null;
}
