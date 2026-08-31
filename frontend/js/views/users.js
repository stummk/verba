// User administration (administrators only): the protection switch, the
// default visibility for new transcripts, and the accounts themselves.
//
// Reached from the settings page. Everything here is guarded on the server as
// well — this view only decides what is worth showing.

import { api } from "../api.js";
import { el, esc, html, toast } from "../dom.js";
import { iconButton } from "../icons.js";
import { t } from "../i18n.js";

let state = { enabled: false, me: null, users: [], defaultVisibility: "private" };

export async function render(view) {
  const [authState, settings] = await Promise.all([
    api.authState(),
    api.getSettings().catch(() => null),
  ]);
  state = {
    enabled: authState.enabled,
    me: authState.user,
    users: authState.enabled ? await api.listUsers().catch(() => []) : [],
    defaultVisibility: settings?.auth?.default_visibility ?? "private",
    settings,
  };

  view.replaceChildren(html`
    <p><a href="#/settings" class="muted small">${t("users.back")}</a></p>
    <h1>${t("users.title")}</h1>
    <div class="card" id="users-access"></div>
    <div class="card" id="users-list-card" hidden>
      <h2>${t("users.accounts")}</h2>
      <p class="muted small">${t("users.accountsIntro")}</p>
      <div id="users-list"></div>
      <div class="actions">
        <button type="button" class="btn primary" id="user-new">${t("users.create")}</button>
      </div>
    </div>
    <dialog id="user-dialog"></dialog>
  `);

  renderAccess();
  renderList();
  el("user-new").onclick = () => openUserDialog(null);
}

// ── the switch ────────────────────────────────────────────────────────

function renderAccess() {
  const host = el("users-access");
  if (state.enabled) {
    host.replaceChildren(html`
      <h2>${t("users.accessTitle")}</h2>
      <p class="badge-ok">${t("users.accessOn")}</p>
      <div class="form-grid">
        <div>
          <label for="default-visibility">${t("users.defaultVisibility")}</label>
          <select id="default-visibility">
            <option value="private">${t("visibility.private")}</option>
            <option value="shared">${t("visibility.shared")}</option>
            <option value="public">${t("visibility.public")}</option>
          </select>
          <p class="hint">${t("users.defaultVisibilityHint")}</p>
        </div>
      </div>
      <div class="actions">
        <button type="button" class="text-btn danger" id="auth-disable">
          ${t("users.disable")}
        </button>
      </div>
      <p class="hint">${t("users.disableHint")}</p>
    `);
    const select = el("default-visibility");
    select.value = state.defaultVisibility;
    select.onchange = async () => {
      try {
        const payload = { ...state.settings };
        payload.auth = { ...payload.auth, default_visibility: select.value };
        await api.updateSettings(payload);
        state.defaultVisibility = select.value;
        state.settings = payload;
        toast(t("settings.saved"));
      } catch (error) {
        toast(error.message);
        select.value = state.defaultVisibility;
      }
    };
    el("auth-disable").onclick = async () => {
      if (!confirm(t("users.disableConfirm"))) return;
      try {
        await api.disableAuth();
        location.reload();
      } catch (error) {
        toast(error.message);
      }
    };
    el("users-list-card").hidden = false;
    return;
  }

  host.replaceChildren(html`
    <h2>${t("users.accessTitle")}</h2>
    <p class="warning-box">${t("users.accessOff")}</p>
    <form id="auth-enable-form" class="form-grid">
      <div>
        <label for="enable-username">${t("login.username")}</label>
        <input id="enable-username" autocomplete="username" required>
      </div>
      <div>
        <label for="enable-password">${t("login.newPassword")}</label>
        <input id="enable-password" type="password" autocomplete="new-password" required>
      </div>
    </form>
    <p class="hint">${t("users.enableHint")}</p>
    <div class="actions">
      <button type="button" class="btn primary" id="auth-enable">${t("users.enable")}</button>
    </div>
  `);
  el("users-list-card").hidden = true;
  el("auth-enable").onclick = async () => {
    const username = el("enable-username").value.trim();
    const password = el("enable-password").value;
    if (!username || !password) return;
    try {
      const result = await api.enableAuth(username, password);
      toast(t("users.enabled", { count: result.adopted_projects }));
      location.reload();
    } catch (error) {
      toast(error.message);
    }
  };
}

// ── the accounts ──────────────────────────────────────────────────────

function roleLabel(user) {
  return t(user.role === "admin" ? "users.roleAdmin" : "users.roleUser");
}

function renderList() {
  const host = el("users-list");
  if (!host) return;
  host.replaceChildren(...state.users.map((user) => {
    const row = document.createElement("div");
    row.className = "user-row";
    const isMe = user.id === state.me?.id;
    // every interpolated value goes through esc() — user names are free text
    row.innerHTML = `
      <div class="user-main">
        <span class="model-name">${esc(user.display_name || user.username)}</span>
        <span class="muted small">${esc(user.username)}</span>
      </div>
      <span class="chip">${esc(roleLabel(user))}</span>
      <span class="muted small">${esc(t("users.owns", { count: user.project_count }))}</span>
      ${user.must_change_password ? `<span class="chip warn">${esc(t("users.pending"))}</span>` : ""}
      ${isMe ? `<span class="chip">${esc(t("users.you"))}</span>` : ""}
    `;
    const actions = document.createElement("span");
    actions.className = "row-actions";
    actions.append(iconButton("edit", t("users.edit"), () => openUserDialog(user)));
    actions.append(iconButton("delete", t("users.delete"), () => confirmDelete(user)));
    row.append(actions);
    return row;
  }));
}

function dialogMarkup(user) {
  const editing = Boolean(user);
  return `
    <form method="dialog" class="dialog-form" id="user-form">
      <h2>${esc(editing ? t("users.editTitle") : t("users.createTitle"))}</h2>
      <label for="user-username">${esc(t("login.username"))}</label>
      <input id="user-username" value="${esc(user?.username ?? "")}"
             ${editing ? "disabled" : "required"}>
      <label for="user-display">${esc(t("users.displayName"))}</label>
      <input id="user-display" value="${esc(user?.display_name ?? "")}">
      <label for="user-role">${esc(t("users.role"))}</label>
      <select id="user-role">
        <option value="user">${esc(t("users.roleUser"))}</option>
        <option value="admin">${esc(t("users.roleAdmin"))}</option>
      </select>
      <p class="hint">${esc(t("users.roleHint"))}</p>
      <label for="user-password">
        ${esc(editing ? t("users.newStartPassword") : t("users.startPassword"))}
      </label>
      <input id="user-password" type="password" autocomplete="new-password"
             ${editing ? "" : "required"}>
      <p class="hint">${esc(t("users.startPasswordHint"))}</p>
      <p class="error" id="user-error" hidden></p>
      <div class="actions">
        <button type="button" class="text-btn" id="user-cancel">${esc(t("common.cancel"))}</button>
        <button type="submit" class="btn primary">${esc(t("common.save"))}</button>
      </div>
    </form>
  `;
}

function openUserDialog(user) {
  const dialog = el("user-dialog");
  dialog.innerHTML = dialogMarkup(user);
  el("user-role").value = user?.role ?? "user";
  el("user-cancel").onclick = () => dialog.close();
  el("user-form").onsubmit = async (event) => {
    event.preventDefault();
    const password = el("user-password").value;
    const payload = {
      display_name: el("user-display").value.trim(),
      role: el("user-role").value,
    };
    try {
      if (user) {
        if (password) payload.password = password;
        await api.updateUser(user.id, payload);
      } else {
        await api.createUser({
          username: el("user-username").value.trim(),
          password,
          ...payload,
        });
      }
      dialog.close();
      await reload();
    } catch (error) {
      const box = el("user-error");
      box.textContent = error.message;
      box.hidden = false;
    }
  };
  dialog.showModal();
}

async function confirmDelete(user) {
  // The strategy is spelled out rather than summarised: what disappears and
  // what changes hands is not something to find out afterwards.
  const message = t("users.deleteConfirm", {
    name: user.display_name || user.username,
    count: user.project_count,
  });
  if (!confirm(message)) return;
  try {
    const result = await api.deleteUser(user.id);
    toast(t("users.deleted", {
      deleted: result.deleted_projects,
      transferred: result.transferred_projects,
    }));
    await reload();
  } catch (error) {
    toast(error.message);
  }
}

async function reload() {
  state.users = await api.listUsers().catch(() => []);
  renderList();
}
