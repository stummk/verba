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
// {enabled, user} plus the name list for the share picker; all empty while the
// user management is off, and then nothing visibility-related is shown at all
let access = { enabled: false, user: null, directory: [] };
// set up once with the dialog; re-applied whenever it is opened
let syncVisibilityDialog = () => {};

export async function render(view, systemStatus) {
  const [projects, types, jobs, authState] = await Promise.all([
    api.listProjects(),
    api.listTypes(),
    api.listJobs(true).catch(() => []),
    api.authState().catch(() => ({ enabled: false, user: null })),
  ]);
  access = {
    enabled: authState.enabled,
    user: authState.user,
    defaultVisibility: authState.default_visibility ?? "private",
    directory: authState.enabled ? await api.userDirectory().catch(() => []) : [],
  };
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
        <div id="create-visibility-field" hidden>
          <label for="project-visibility">${t("visibility.label")}</label>
          <select id="project-visibility">
            <option value="private">${t("visibility.private")}</option>
            <option value="shared">${t("visibility.shared")}</option>
            <option value="public">${t("visibility.public")}</option>
          </select>
          <p class="hint">${t("visibility.createHint")}</p>
        </div>
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
    <dialog id="visibility-dialog">
      <h2>${t("visibility.title")}</h2>
      <form id="visibility-form" method="dialog">
        <div class="segmented" role="radiogroup" id="visibility-choice">
          <label><input type="radio" name="visibility" value="private">
            <span>${t("visibility.private")}</span></label>
          <label><input type="radio" name="visibility" value="shared">
            <span>${t("visibility.shared")}</span></label>
          <label><input type="radio" name="visibility" value="public">
            <span>${t("visibility.public")}</span></label>
        </div>
        <p class="hint" id="visibility-hint"></p>
        <div id="visibility-people" hidden>
          <label for="visibility-users">${t("visibility.people")}</label>
          <select id="visibility-users" multiple size="6"></select>
          <p class="hint">${t("visibility.peopleHint")}</p>
        </div>
        <div class="actions">
          <button type="submit">${t("common.save")}</button>
          <button type="button" class="text-btn" id="visibility-cancel">
            ${t("common.cancel")}
          </button>
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

  // the setup is an administrator's job — a normal user can only read the
  // reminder and then be refused by every button behind it
  if (!ready && (!access.enabled || access.user?.role === "admin")) {
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
  if (access.enabled) {
    el("create-visibility-field").hidden = false;
    el("project-visibility").value = access.defaultVisibility;
  }
  el("create-cancel").onclick = () => createDialog.close();
  el("create-project").onsubmit = async (event) => {
    event.preventDefault();
    const name = el("project-name").value.trim() || defaultProjectName();
    const typeId = el("project-type").value ? Number(el("project-type").value) : null;
    try {
      const visibility = access.enabled ? el("project-visibility").value : "";
      const created = await api.createProject(name, typeId, visibility);
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

  bindVisibilityDialog();

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

// ── visibility ────────────────────────────────────────────────────────

function canAdminister(project) {
  if (!access.enabled) return false; // nothing to set: everything is reachable
  return access.user?.role === "admin" || project.owner_id === access.user?.id;
}

function visibilityLabel(project) {
  return t(`visibility.${project.visibility || "public"}`);
}

function bindVisibilityDialog() {
  const dialog = el("visibility-dialog");
  const radios = [...dialog.querySelectorAll('input[name="visibility"]')];
  const people = el("visibility-people");
  const hint = el("visibility-hint");

  function sync() {
    const value = radios.find((radio) => radio.checked)?.value ?? "private";
    people.hidden = value !== "shared";
    hint.textContent = t(`visibility.${value}Hint`);
  }
  radios.forEach((radio) => radio.addEventListener("change", sync));

  el("visibility-cancel").onclick = () => dialog.close();
  el("visibility-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!currentProjectId) return;
    const value = radios.find((radio) => radio.checked)?.value ?? "private";
    const userIds = [...el("visibility-users").selectedOptions].map((o) => Number(o.value));
    try {
      await api.setVisibility(currentProjectId, value, userIds);
      toast(t("visibility.saved"));
      dialog.close();
      await refreshProjects();
    } catch (error) {
      toast(error.message);
    }
  };
  syncVisibilityDialog = sync;
}

async function openVisibilityDialog(project) {
  currentProjectId = project.id;
  const dialog = el("visibility-dialog");
  const current = project.visibility || "public";
  for (const radio of dialog.querySelectorAll('input[name="visibility"]')) {
    radio.checked = radio.value === current;
  }
  // the share list is not part of the overview payload — fetch the one project
  const detail = await api.getProject(project.id).catch(() => null);
  const shared = new Set(detail?.shared_with ?? []);
  const select = el("visibility-users");
  select.replaceChildren(
    ...access.directory
      .filter((person) => person.id !== project.owner_id)
      .map((person) => {
        const option = new Option(person.display_name || person.username, String(person.id));
        option.selected = shared.has(person.id);
        return option;
      })
  );
  syncVisibilityDialog();
  dialog.showModal();
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
      if (access.enabled) {
        const visibilityBadge = document.createElement("span");
        visibilityBadge.className = `badge badge-visibility ${project.visibility}`;
        visibilityBadge.textContent = visibilityLabel(project);
        // whose it is matters as soon as more than one person works here
        visibilityBadge.title = project.owner_name
          ? t("visibility.ownedBy", { name: project.owner_name })
          : t("visibility.ownerless");
        right.append(visibilityBadge);
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
      actions.append(renameBtn);
      if (canAdminister(project)) {
        actions.append(iconButton("lock", t("visibility.title"), (event) => {
          event.preventDefault();
          event.stopPropagation();
          openVisibilityDialog(project);
        }));
      }
      actions.append(deleteBtn);
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
