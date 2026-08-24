// First-run setup wizard: shows system checks and runs the auto-installation.

import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { t } from "../i18n.js";
import { on } from "../ws.js";

let unsubscribe = null;

export async function render(view) {
  const status = await api.systemStatus();
  view.replaceChildren(html`
    <h1>${t("setup.title")}</h1>
    <div class="card">
      <p class="muted">${t("setup.intro")}</p>
      <ul class="checklist" id="setup-checklist"></ul>
    </div>
    <div class="card" id="setup-progress-card" hidden>
      <h2 id="setup-step">${t("setup.running", { step: "…" })}</h2>
      <div class="progressbar"><div id="setup-bar"></div></div>
      <div class="setup-log" id="setup-log"></div>
    </div>
    <div class="actions">
      <button id="setup-run">${t("setup.start")}</button>
      <button id="setup-skip" class="text-btn">${t("setup.later")}</button>
    </div>
  `);

  renderChecklist(status.checks);

  const runButton = el("setup-run");
  const progressCard = el("setup-progress-card");

  if (status.ready) {
    runButton.textContent = t("setup.ready");
    runButton.onclick = () => { location.hash = "#/"; };
    el("setup-skip").hidden = true;
  } else {
    runButton.onclick = async () => {
      runButton.disabled = true;
      progressCard.hidden = false;
      try {
        await api.runSetup(true);
      } catch (error) {
        toast(t("setup.startError", { message: error.message }));
        runButton.disabled = false;
      }
    };
    el("setup-skip").onclick = () => { location.hash = "#/"; };
  }

  if (status.setup?.running) {
    runButton.disabled = true;
    progressCard.hidden = false;
    applyProgress(status.setup);
  }

  unsubscribe?.();
  unsubscribe = on("setup.progress", async (progress) => {
    progressCard.hidden = false;
    applyProgress(progress);
    if (!progress.running) {
      const fresh = await api.systemStatus();
      renderChecklist(fresh.checks);
      if (fresh.ready && !progress.error) {
        toast(t("setup.completedToast"));
        setTimeout(() => { location.hash = "#/"; }, 1200);
      } else {
        runButton.disabled = false;
        runButton.textContent = t("common.retry");
      }
    }
  });
}

function renderChecklist(checks) {
  const list = el("setup-checklist");
  if (!list) return;
  list.replaceChildren(
    ...checks.map((check) => {
      const item = document.createElement("li");
      item.className = [
        check.ok ? "check-ok" : "check-fail",
        check.required ? "" : "check-optional",
      ].join(" ");
      item.append(
        Object.assign(document.createElement("span"), {
          className: "check-icon",
          textContent: check.ok ? "✔" : check.required ? "✖" : "○",
        }),
        Object.assign(document.createElement("span"), {
          className: "check-label",
          textContent: check.label,
        }),
        Object.assign(document.createElement("span"), {
          className: "check-detail",
          textContent: check.detail,
        }),
      );
      return item;
    })
  );
}

function applyProgress(progress) {
  const bar = el("setup-bar");
  const step = el("setup-step");
  const log = el("setup-log");
  if (!bar) return;
  bar.style.width = `${progress.percent ?? 0}%`;
  if (progress.error) {
    step.textContent = t("setup.failed");
  } else if (progress.running) {
    step.textContent = t("setup.running", {
      step: progress.step,
      detail: progress.detail || "",
      percent: progress.percent ?? 0,
    });
  } else {
    step.textContent = t("setup.done");
  }
  log.textContent = (progress.log ?? []).join("\n");
  log.scrollTop = log.scrollHeight;
}
