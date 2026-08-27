// Shared presentation of background jobs: the top bar, the transcript view and
// the settings all have to answer the same question — which step is running on
// which file, and how far along is it.

import { iconButton } from "./icons.js";
import { t } from "./i18n.js";

export function isActive(job) {
  return job.status === "queued" || job.status === "running";
}

export function jobStepLabel(job) {
  return t(`jobKind.${job.kind}`);
}

// "Bereinigung — lied.mp3: 2/5 · 40 %" instead of "Job #7 (llm_process): 40%".
// The file name is added only when the job's own message does not carry it.
export function jobStatusLine(job) {
  const message = job.message ?? "";
  const file = job.filename && !message.includes(job.filename) ? job.filename : "";
  const step = jobStepLabel(job);
  if (job.status === "queued") {
    const detail = file || message;
    // a queued transcript-wide job has neither file nor message yet
    return detail ? t("app.jobQueued", { step, detail }) : t("app.jobQueuedPlain", { step });
  }
  return t("app.jobRunning", {
    step,
    detail: [file, message].filter(Boolean).join(": "),
    percent: job.progress ?? 0,
  });
}

// A progress card for jobs that belong to no single file row — the PDF export
// of a whole transcript, a reindex, a workspace move. Without it those jobs
// would only ever show up in the status line of the top bar.
export function jobCard(job, { onCancel = null } = {}) {
  const card = document.createElement("div");
  card.className = "card job-card";
  card.dataset.jobId = job.id;

  const head = document.createElement("div");
  head.className = "job-card-head";
  head.append(
    Object.assign(document.createElement("strong"), { textContent: jobStepLabel(job) }),
    Object.assign(document.createElement("span"), { className: "spacer" }),
    Object.assign(document.createElement("span"), {
      className: "small muted",
      textContent: job.status === "queued" ? t("status.queued") : `${job.progress ?? 0} %`,
    }),
  );
  if (onCancel) head.append(iconButton("stop", t("common.cancel"), () => onCancel(job)));

  const bar = document.createElement("div");
  bar.className = "progressbar small-bar";
  bar.innerHTML = `<div style="width:${job.progress ?? 0}%"></div>`;

  const message = document.createElement("div");
  message.className = "small muted job-message";
  message.textContent = job.message ?? "";

  card.append(head, bar, message);
  return card;
}

// Keep a host element in sync with the active jobs it is responsible for.
// `filter` decides which jobs belong here, so each view stays specific.
export function jobCardHost(host, { filter, onCancel = null }) {
  const jobs = new Map();

  function render() {
    host.replaceChildren(...[...jobs.values()].map((job) => jobCard(job, { onCancel })));
    host.hidden = jobs.size === 0;
  }

  render();
  return {
    apply(job) {
      if (!filter(job)) return;
      if (isActive(job)) jobs.set(job.id, job);
      else jobs.delete(job.id);
      render();
    },
  };
}
