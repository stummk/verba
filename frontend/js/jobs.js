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

// A job in the heading of the section it belongs to (the export section of a
// transcript): the step with its message, then the percentage with a circular
// indicator and the stop button — no bar, and it reads the same open or closed.
export function jobLine(job, { onCancel = null } = {}) {
  const row = document.createElement("span");
  row.className = "job-line";
  row.dataset.jobId = job.id;

  const text = document.createElement("span");
  text.className = "job-line-text";
  text.append(
    Object.assign(document.createElement("strong"), { textContent: jobStepLabel(job) }),
    Object.assign(document.createElement("span"), {
      className: "small muted job-message",
      textContent: job.message ?? "",
    }),
  );

  row.append(text);
  row.append(Object.assign(document.createElement("span"), {
    className: "small muted job-line-percent",
    textContent: job.status === "queued" ? t("status.queued") : `${job.progress ?? 0} %`,
  }));
  row.append(progressSpinner(job));
  if (onCancel) row.append(iconButton("stop", t("common.cancel"), () => onCancel(job)));
  // the heading is a <summary>: a click anywhere in it would fold the section
  // away, and stopping the export is not a request to do that
  row.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
  });
  return row;
}

// The ring says the same as the number next to it, so it carries no label of
// its own; a queued job has no percentage yet and spins instead.
function progressSpinner(job) {
  const spinner = document.createElement("span");
  const queued = job.status === "queued";
  spinner.className = queued ? "job-spinner waiting" : "job-spinner";
  spinner.style.setProperty("--progress", String(queued ? 25 : (job.progress ?? 0)));
  spinner.setAttribute("aria-hidden", "true");
  return spinner;
}

// Keep a host element in sync with the active jobs it is responsible for.
// `filter` decides which jobs belong here, so each view stays specific, and
// `item` chooses the presentation.
export function jobCardHost(host, { filter, onCancel = null, item = jobCard }) {
  const jobs = new Map();

  function render() {
    host.replaceChildren(...[...jobs.values()].map((job) => item(job, { onCancel })));
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
