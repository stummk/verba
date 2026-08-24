// App bootstrap: i18n, hash router, adaptive shell (top bar, nav, FAB).

import { api } from "./api.js";
import { el } from "./dom.js";
import { initI18n, t } from "./i18n.js";
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

function parseHash() {
  const segments = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { route: segments[0] || "dashboard", params: segments.slice(1) };
}

let systemStatus = null;

async function navigate() {
  const parsed = parseHash();
  const route = routes[parsed.route] ? parsed.route : "dashboard";
  const config = routes[route];

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

function bindShell() {
  const indicator = el("ws-indicator");
  const engineStatus = el("engine-status");

  let wasOffline = false;
  ws.on("connection", ({ online }) => {
    indicator.className = `dot ${online ? "online" : "offline"}`;
    indicator.title = online ? t("app.connected") : t("app.disconnected");
    if (online && wasOffline) navigate(); // server is back — re-render the view
    wasOffline = !online;
  });
  ws.on("setup.progress", (progress) => {
    engineStatus.textContent = progress.running
      ? t("app.setupProgress", { step: progress.step, percent: progress.percent })
      : "";
  });
  ws.on("engine.status", ({ engine, state, detail }) => {
    engineStatus.textContent = state === "idle" ? "" : `${engine}: ${detail || state}`;
  });
  ws.on("job.update", (job) => {
    if (job.status === "running") {
      engineStatus.textContent = t("app.jobProgress", {
        id: job.id, kind: job.kind, percent: job.progress,
      });
    } else {
      engineStatus.textContent = "";
    }
  });

  // browser-level offline/online transitions re-render the current view
  window.addEventListener("online", navigate);
  window.addEventListener("offline", () => {
    indicator.className = "dot offline";
    indicator.title = t("app.disconnected");
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
    if (!systemStatus.ready && !systemStatus.setup_completed && !location.hash) {
      location.hash = "#/setup";
    }
  } catch { /* connection indicator shows the state */ }

  window.addEventListener("hashchange", navigate);
  await navigate();

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

init();
