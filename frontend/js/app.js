// App bootstrap: i18n, hash router, adaptive shell (top bar, nav, FAB).

import { api } from "./api.js";
import { el, html, toast } from "./dom.js";
import { initI18n, t } from "./i18n.js";
import { isActive, jobStatusLine, jobStepLabel } from "./jobs.js";
import * as ws from "./ws.js";
import * as dashboard from "./views/dashboard.js";
import * as docs from "./views/docs.js";
import * as editor from "./views/editor.js";
import { renderLogin, renderPasswordChange } from "./views/login.js";
import * as project from "./views/project.js";
import * as search from "./views/search.js";
import * as settings from "./views/settings.js";
import * as setup from "./views/setup.js";
import * as types from "./views/types.js";
import * as users from "./views/users.js";

const routes = {
  dashboard: { module: dashboard, title: "dashboard.title", fab: true },
  project: { module: project, title: null, fab: true },
  editor: { module: editor, title: null, fab: false },
  types: { module: types, title: "types.title", fab: true },
  search: { module: search, title: "search.title", fab: false },
  settings: { module: settings, title: "settings.title", fab: false },
  setup: { module: setup, title: "setup.title", fab: false, admin: true },
  docs: { module: docs, title: "docs.title", fab: false },
  users: { module: users, title: "users.title", fab: false, admin: true },
};

// Views a normal user has no business in — the server refuses them anyway,
// this only keeps the app from showing a page made of error cards.
const ADMIN_ROUTES = ["types", "users"];

// How many jobs the status line names before it summarises the rest.
const MAX_STATUS_JOBS = 2;

function byRunningFirst(a, b) {
  if (a.status !== b.status) return a.status === "running" ? -1 : 1;
  return a.id - b.id;
}

function isAdmin() {
  return !authState.enabled || authState.user?.role === "admin";
}

function parseHash() {
  const segments = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { route: segments[0] || "dashboard", params: segments.slice(1) };
}

let systemStatus = null;
// {enabled, user} — user is null while the management is off, so `isAdmin()`
// is true then and every role check below reads "everything allowed"
let authState = { enabled: false, user: null };
// the view currently on screen, and a counter that invalidates a render whose
// navigation was overtaken by the next one
let activeModule = null;
let navigation = 0;
// the very first run shows the wizard alone: no tabs to wander off into until
// it is finished or skipped. Opening it again later is a normal view.
let firstRunPending = false;

async function navigate() {
  const parsed = parseHash();
  let route = routes[parsed.route] ? parsed.route : "dashboard";
  if (routes[route].admin && !isAdmin()) route = "dashboard";
  const config = routes[route];
  const token = ++navigation;

  // The outgoing view releases its WebSocket subscriptions, timers and window
  // handlers *before* the next one is built — otherwise every visited view
  // keeps redrawing into a DOM that is long gone, which is what made leaving
  // the editor feel like a freeze.
  if (activeModule && activeModule !== config.module) activeModule.destroy?.();
  activeModule = config.module;

  if (route !== "setup") firstRunPending = false; // skipped or finished
  const firstRun = firstRunPending && route === "setup";
  el("main-nav").hidden = firstRun;
  document.body.classList.toggle("nav-hidden", firstRun);

  document.querySelectorAll(".nav-item").forEach((link) => {
    link.hidden = ADMIN_ROUTES.includes(link.dataset.route) && !isAdmin();
    link.classList.toggle(
      "active",
      link.dataset.route === route ||
        (["project", "editor"].includes(route) && link.dataset.route === "dashboard") ||
        (route === "docs" && link.dataset.route === "settings")
    );
  });
  el("page-title").textContent = config.title ? t(config.title) : t("app.title");
  el("fab").hidden = !config.fab;

  const view = el("view");
  // the click gets an answer immediately: the old view goes, a spinner takes
  // over (it fades in late enough that a fast render never flashes it)
  view.replaceChildren(html`<div class="view-loading" aria-hidden="true"></div>`);
  try {
    await config.module.render(view, systemStatus, parsed.params);
    if (token !== navigation) return; // a newer navigation owns the view now
  } catch (error) {
    if (token !== navigation) return;
    view.innerHTML = "";
    const card = document.createElement("div");
    card.className = "card";
    // a fetch network failure is a TypeError — server unreachable = offline
    card.textContent = !navigator.onLine || error instanceof TypeError
      ? t("app.offline")
      : t("app.viewError", { message: error.message });
    view.appendChild(card);
  }
}

function showClosedNotice() {
  document.body.classList.add("nav-hidden");
  el("main-nav").hidden = true;
  el("shutdown").hidden = true;
  el("fab").hidden = true;
  const card = document.createElement("div");
  card.className = "card";
  card.textContent = t("app.closedNotice");
  el("view").replaceChildren(card);
}

function bindShell() {
  const indicator = el("ws-indicator");
  const engineStatus = el("engine-status");
  const shutdown = el("shutdown");

  shutdown.onclick = async () => {
    shutdown.disabled = true;
    await api.shutdown().catch(() => {});
    // works when the browser was opened by us; otherwise the tab stays and
    // says so, because the server behind it is gone either way
    window.close();
    setTimeout(showClosedNotice, 400);
  };

  let wasOffline = false;
  ws.on("connection", ({ online }) => {
    indicator.className = `dot ${online ? "online" : "offline"}`;
    indicator.title = online ? t("app.connected") : t("app.disconnected");
    if (online && wasOffline) navigate(); // server is back — re-render the view
    wasOffline = !online;
  });
  // The status line has three sources — setup, jobs, engines — which used to
  // overwrite each other. They are kept apart and composed on every change,
  // so two lanes running in parallel are both visible.
  const runningJobs = new Map();
  let setupLine = "";
  let engineLine = "";

  function renderStatus() {
    const parts = [];
    if (setupLine) parts.push(setupLine);
    const jobs = [...runningJobs.values()].sort(byRunningFirst);
    for (const job of jobs.slice(0, MAX_STATUS_JOBS)) parts.push(jobStatusLine(job));
    if (jobs.length > MAX_STATUS_JOBS) {
      parts.push(t("app.jobsMore", { count: jobs.length - MAX_STATUS_JOBS }));
    }
    // an engine message (loading a model, LLM busy) only matters while no job
    // is telling the more specific story
    if (!parts.length && engineLine) parts.push(engineLine);
    const line = parts.join("   ·   ");
    engineStatus.textContent = line;
    engineStatus.title = line; // the bar truncates; the tooltip has it all
  }

  ws.on("setup.progress", (progress) => {
    setupLine = progress.running
      ? t("app.setupProgress", {
        step: progress.step,
        detail: progress.detail || "",
        percent: progress.percent,
      })
      : "";
    renderStatus();
  });
  ws.on("engine.status", ({ engine, state, detail }) => {
    engineLine = state === "idle" ? "" : `${t(`engine.${engine}`)}: ${detail || state}`;
    renderStatus();
    // a model that ran out of memory must not hide in a status line that the
    // next event overwrites
    if (state === "error" && detail) toast(`${t(`engine.${engine}`)}: ${detail}`);
  });
  ws.on("job.update", (job) => {
    if (isActive(job)) runningJobs.set(job.id, job);
    else runningJobs.delete(job.id);
    renderStatus();
    // A failed step used to end in silence: the row lost its progress bar and
    // nothing said why. Only jobs whose file keeps its status (AI processing,
    // export) would otherwise have no place to show the error at all.
    if (job.status === "failed") {
      const detail = [job.filename, job.error].filter(Boolean).join(": ");
      toast(t("app.jobFailed", { step: jobStepLabel(job), detail }));
    }
  });
  // jobs that were already running when this tab opened (or a reload)
  api.listJobs(true).then((jobs) => {
    for (const job of jobs) runningJobs.set(job.id, job);
    renderStatus();
  }).catch(() => {});

  // A new release announces itself once the background check finds one. Only
  // an administrator can install it, so only they are told; the reminder on
  // the start page repeats it on every later visit.
  ws.on("update.available", (info) => {
    if (!isAdmin()) return;
    // the status may have failed to load — the toast is the message either way
    if (systemStatus) {
      systemStatus.update_available = true;
      systemStatus.update_version = info.version;
    }
    toast(t("app.updateAvailable", { version: info.version }));
  });

  // browser-level offline/online transitions re-render the current view
  window.addEventListener("online", navigate);
  window.addEventListener("offline", () => {
    indicator.className = "dot offline";
    indicator.title = t("app.disconnected");
  });

  // views (the setup wizard) publish a fresh status after they changed it
  window.addEventListener("system:status", (event) => {
    systemStatus = event.detail;
    el("shutdown").hidden = !systemStatus.desktop_mode || !isAdmin();
    if (systemStatus.setup_completed) firstRunPending = false;
    navigate();
  });

  // FAB is context-sensitive: views listen for this event
  el("fab").onclick = () => window.dispatchEvent(new CustomEvent("fab:click"));

  // Drag & drop belongs to the import step alone. Everywhere else a dropped
  // file would make the browser leave the app and open that file, so the
  // default is swallowed — without any highlight, so nothing invites a drop.
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (event) => {
      if (event.target.closest?.(".drop-zone")) return; // the zone handles it
      event.preventDefault();
      if (type === "drop") event.dataTransfer.dropEffect = "none";
    });
  }
}

// The last known interface language, so the login screen — which cannot read
// the settings yet — comes up in the language the user chose.
const LANGUAGE_KEY = "verba.language";

async function loadLanguage() {
  let uiLanguage = localStorage.getItem(LANGUAGE_KEY) || "de";
  // No session means no settings to read — asking anyway would only produce a
  // 401 in the console of every login screen. The remembered choice is what
  // the login screen is rendered in.
  if (!authState.enabled || authState.user) {
    try {
      const stored = await api.getSettings();
      uiLanguage = stored.general?.ui_language || uiLanguage;
      localStorage.setItem(LANGUAGE_KEY, uiLanguage);
    } catch { /* not reachable — the stored value has to do */ }
  }
  await initI18n(uiLanguage);
}

/** Show the sign-in screen instead of the app; booting continues after it. */
function showLoginScreen() {
  document.body.classList.add("nav-hidden", "signed-out");
  el("main-nav").hidden = true;
  el("fab").hidden = true;
  el("shutdown").hidden = true;
  el("logout").hidden = true;
  el("current-user").textContent = "";
  el("page-title").textContent = t("app.title");
  renderLogin(el("view"), () => location.reload());
}

function showPasswordChangeScreen() {
  document.body.classList.add("nav-hidden");
  el("main-nav").hidden = true;
  el("fab").hidden = true;
  renderPasswordChange(el("view"), () => location.reload());
}

function bindAuthShell() {
  const logout = el("logout");
  logout.onclick = async () => {
    await api.logout().catch(() => {});
    location.reload();
  };
  // A session can end while the app is open (expiry, an administrator
  // deleting the account, the protection being switched on elsewhere). The
  // API client raises this on the first 401 so the screen changes instead of
  // filling up with error cards.
  window.addEventListener("auth:required", () => {
    if (!document.body.classList.contains("signed-out")) showLoginScreen();
  });
  window.addEventListener("auth:passwordChange", showPasswordChangeScreen);
}

function applyIdentity() {
  const user = authState.user;
  el("logout").hidden = !user;
  el("current-user").textContent = user ? (user.display_name || user.username) : "";
}

async function init() {
  authState = await api.authState().catch(() => ({ enabled: false, user: null }));
  await loadLanguage();
  bindAuthShell();

  if (authState.enabled && !authState.user) {
    showLoginScreen();
    return; // no WebSocket, no views: there is nothing this visitor may see
  }
  if (authState.user?.must_change_password) {
    showPasswordChangeScreen();
    return;
  }
  applyIdentity();

  bindShell();
  ws.connect();

  try {
    systemStatus = await api.systemStatus();
    el("shutdown").hidden = !systemStatus.desktop_mode || !isAdmin();
    // Installing components and finishing the setup are administrator
    // endpoints, so a normal user must never be sent into the wizard — they
    // would only collect refusals from a screen they cannot leave forwards.
    if (!systemStatus.ready && !systemStatus.setup_completed && isAdmin()) {
      firstRunPending = true;
      if (!location.hash) location.hash = "#/setup";
    }
  } catch { /* connection indicator shows the state */ }

  window.addEventListener("hashchange", navigate);
  await navigate();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

init();
