// App bootstrap: i18n, hash router, adaptive shell (top bar, nav, FAB).

import { api } from "./api.js";
import { el } from "./dom.js";
import { initI18n, t } from "./i18n.js";
import { isActive, jobStatusLine } from "./jobs.js";
import * as ws from "./ws.js";
import * as dashboard from "./views/dashboard.js";
import * as docs from "./views/docs.js";
import * as editor from "./views/editor.js";
import * as project from "./views/project.js";
import * as search from "./views/search.js";
import * as settings from "./views/settings.js";
import * as setup from "./views/setup.js";
import * as types from "./views/types.js";

const routes = {
  dashboard: { render: dashboard.render, title: "dashboard.title", fab: true },
  project: { render: project.render, title: null, fab: true },
  editor: { render: editor.render, title: null, fab: false },
  types: { render: types.render, title: "types.title", fab: true },
  search: { render: search.render, title: "search.title", fab: false },
  settings: { render: settings.render, title: "settings.title", fab: false },
  setup: { render: setup.render, title: "setup.title", fab: false },
  docs: { render: docs.render, title: "docs.title", fab: false },
};

// How many jobs the status line names before it summarises the rest.
const MAX_STATUS_JOBS = 2;

function byRunningFirst(a, b) {
  if (a.status !== b.status) return a.status === "running" ? -1 : 1;
  return a.id - b.id;
}

function parseHash() {
  const segments = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { route: segments[0] || "dashboard", params: segments.slice(1) };
}

let systemStatus = null;
// the very first run shows the wizard alone: no tabs to wander off into until
// it is finished or skipped. Opening it again later is a normal view.
let firstRunPending = false;

async function navigate() {
  const parsed = parseHash();
  const route = routes[parsed.route] ? parsed.route : "dashboard";
  const config = routes[route];

  if (route !== "setup") firstRunPending = false; // skipped or finished
  const firstRun = firstRunPending && route === "setup";
  el("main-nav").hidden = firstRun;
  document.body.classList.toggle("nav-hidden", firstRun);

  document.querySelectorAll(".nav-item").forEach((link) => {
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
  try {
    await config.render(view, systemStatus, parsed.params);
  } catch (error) {
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
  });
  ws.on("job.update", (job) => {
    if (isActive(job)) runningJobs.set(job.id, job);
    else runningJobs.delete(job.id);
    renderStatus();
  });
  // jobs that were already running when this tab opened (or a reload)
  api.listJobs(true).then((jobs) => {
    for (const job of jobs) runningJobs.set(job.id, job);
    renderStatus();
  }).catch(() => {});

  // browser-level offline/online transitions re-render the current view
  window.addEventListener("online", navigate);
  window.addEventListener("offline", () => {
    indicator.className = "dot offline";
    indicator.title = t("app.disconnected");
  });

  // views (the setup wizard) publish a fresh status after they changed it
  window.addEventListener("system:status", (event) => {
    systemStatus = event.detail;
    el("shutdown").hidden = !systemStatus.desktop_mode;
    if (systemStatus.setup_completed) firstRunPending = false;
    navigate();
  });

  // FAB is context-sensitive: views listen for this event
  el("fab").onclick = () => window.dispatchEvent(new CustomEvent("fab:click"));
}

async function init() {
  let uiLanguage = "de";
  try {
    const stored = await api.getSettings();
    uiLanguage = stored.general?.ui_language || "de";
  } catch { /* backend not reachable yet — fall back to default */ }
  await initI18n(uiLanguage);

  bindShell();
  ws.connect();

  try {
    systemStatus = await api.systemStatus();
    el("shutdown").hidden = !systemStatus.desktop_mode;
    if (!systemStatus.ready && !systemStatus.setup_completed) {
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
