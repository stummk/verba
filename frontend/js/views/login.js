// The screen in front of everything else: sign in, or replace a start password.
//
// This is not a route — app.js renders it instead of the shell whenever the
// user management is on and the session is missing or still owes a password
// change. Leaving it is what boots the rest of the app.

import { api } from "../api.js";
import { el, html, raw } from "../dom.js";
import { t } from "../i18n.js";

// `inner` is markup built here, not user input — raw() keeps html`` from
// escaping it into visible source text.
function card(inner) {
  return html`
    <div class="login-screen">
      <div class="card login-card">
        <img class="login-logo" src="/icons/icon.svg" alt="" width="48" height="48">
        <h1>${t("app.title")}</h1>
        ${raw(inner)}
      </div>
    </div>
  `;
}

function showError(message) {
  const box = el("login-error");
  box.textContent = message;
  box.hidden = !message;
}

/** Sign-in form. Resolves once a session exists; the caller reboots the app. */
export function renderLogin(view, onSuccess) {
  view.replaceChildren(
    card(`
      <p class="muted">${t("login.intro")}</p>
      <form id="login-form" autocomplete="on">
        <label for="login-username">${t("login.username")}</label>
        <input id="login-username" name="username" autocomplete="username" required autofocus>
        <label for="login-password">${t("login.password")}</label>
        <input id="login-password" name="password" type="password"
               autocomplete="current-password" required>
        <p class="error" id="login-error" hidden></p>
        <button class="btn primary block" type="submit" id="login-submit">
          ${t("login.submit")}
        </button>
      </form>
    `)
  );

  el("login-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = el("login-submit");
    button.disabled = true;
    showError("");
    try {
      await api.login(el("login-username").value.trim(), el("login-password").value);
      onSuccess();
    } catch (error) {
      showError(error.message);
      el("login-password").value = "";
      el("login-password").focus();
    } finally {
      button.disabled = false;
    }
  };
}

/**
 * Forced change of a start password.
 *
 * The backend refuses every other route until this is done, so the screen has
 * no way past it except signing out.
 */
export function renderPasswordChange(view, onSuccess) {
  view.replaceChildren(
    card(`
      <h2>${t("login.changeTitle")}</h2>
      <p class="muted">${t("login.changeIntro")}</p>
      <form id="login-form">
        <label for="login-current">${t("login.currentPassword")}</label>
        <input id="login-current" type="password" autocomplete="current-password" required>
        <label for="login-new">${t("login.newPassword")}</label>
        <input id="login-new" type="password" autocomplete="new-password" required>
        <label for="login-repeat">${t("login.repeatPassword")}</label>
        <input id="login-repeat" type="password" autocomplete="new-password" required>
        <p class="error" id="login-error" hidden></p>
        <button class="btn primary block" type="submit" id="login-submit">
          ${t("login.changeSubmit")}
        </button>
        <button class="text-btn block" type="button" id="login-signout">
          ${t("app.logout")}
        </button>
      </form>
    `)
  );

  el("login-signout").onclick = async () => {
    await api.logout().catch(() => {});
    location.reload();
  };

  el("login-form").onsubmit = async (event) => {
    event.preventDefault();
    if (el("login-new").value !== el("login-repeat").value) {
      showError(t("login.passwordsDiffer"));
      return;
    }
    const button = el("login-submit");
    button.disabled = true;
    showError("");
    try {
      await api.changePassword(el("login-current").value, el("login-new").value);
      onSuccess();
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
    }
  };
}
