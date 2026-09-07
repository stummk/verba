// The three steps a file goes through — transcription, AI cleanup, translation
// — as one round badge each, always all three, always in the same order.
//
// A row that only showed what had already happened looked identical before and
// after a step, and said nothing about what was still missing. The badge says
// all of it in one place: grey means the step has not run, a ring filling with
// blue means it is running right now, green means it is done. The words are in
// the tooltip, so a list of twenty files stays readable.

import { iconSvg } from "./icons.js";
import { t } from "./i18n.js";

// Every kind of job that produces the transcript of a file.
const TRANSCRIBE_KINDS = ["transcribe", "transcribe_range", "api_transcribe"];

// The badge order is the order of the workflow, which is also the order the
// pipeline runs its steps in.
const STEPS = [
  { key: "transcribe", icon: "speechToText" },
  { key: "cleanup", icon: "spellcheck" },
  { key: "translation", icon: "translate" },
];

// The pipeline payload calls the translation step "translate", the derived
// text it leaves behind is a "translation" — one name per side of the fence.
const PAYLOAD_STEP = { cleanup: "cleanup", translation: "translate" };

/**
 * The badges of one file.
 *
 * @param {object} fileRow the file, with `status` and `derived_kinds`
 * @param {{job?: object|null, failed?: object|null, detail?: string,
 *          queuePosition?: number|null}} context
 *   `job` is the active job of this file, `failed` the last one that failed,
 *   `detail` the message the running job reports (it lands in the tooltip),
 *   `queuePosition` the place in the queue while it waits.
 * @returns {HTMLElement}
 */
export function stepBadges(fileRow, context = {}) {
  const wrap = document.createElement("div");
  wrap.className = "file-card-steps";
  for (const step of STEPS) {
    wrap.append(badge(step, stepState(step.key, fileRow, context), context));
  }
  return wrap;
}

function stepState(key, fileRow, { job, failed }) {
  if (key === "transcribe") return transcribeState(fileRow, job, failed);
  return llmState(key, fileRow, job, failed);
}

function transcribeState(fileRow, job, failed) {
  if (job && TRANSCRIBE_KINDS.includes(job.kind)) {
    return job.status === "queued"
      ? { state: "queued" }
      : { state: "running", percent: job.progress ?? 0 };
  }
  if (fileRow.status === "transcribing") return { state: "running", percent: 0 };
  if (fileRow.status === "done") return { state: "done" };
  if (fileRow.status === "failed") return { state: "failed" };
  if (failed && TRANSCRIBE_KINDS.includes(failed.kind)) return { state: "failed" };
  return { state: "idle" };
}

function llmState(key, fileRow, job, failed) {
  // A running step wins over a finished one: a second cleanup of an already
  // cleaned file has to show its progress, not the green of the first run.
  const running = job?.kind === "llm_process" ? llmStepState(job, key) : null;
  if (running) return running;
  const derived = (fileRow.derived_kinds ?? "").split(",").filter(Boolean);
  if (derived.includes(key)) return { state: "done" };
  if (failed?.kind === "llm_process" && jobSteps(failed).includes(PAYLOAD_STEP[key])) {
    return { state: "failed" };
  }
  return { state: "idle" };
}

/**
 * Where one step stands inside a running pipeline job.
 *
 * The job handler splits its progress evenly over its steps (cleanup 0–50,
 * translation 50–100 for a run that does both), so the job's percentage says
 * both which step is running and how far along it is.
 */
function llmStepState(job, key) {
  const steps = jobSteps(job);
  const index = steps.indexOf(PAYLOAD_STEP[key]);
  if (index < 0) return null; // this job does not do that step at all
  if (job.status === "queued") return { state: "queued" };
  const lo = (100 * index) / steps.length;
  const hi = (100 * (index + 1)) / steps.length;
  const progress = job.progress ?? 0;
  if (progress >= hi) return { state: "done" };
  if (progress < lo) return { state: "queued" }; // a later step of this run
  return { state: "running", percent: Math.round(((progress - lo) / (hi - lo)) * 100) };
}

function jobSteps(job) {
  const payload = typeof job.payload === "string" ? parsePayload(job.payload) : (job.payload ?? {});
  const steps = payload.steps;
  return Array.isArray(steps) && steps.length ? steps : ["cleanup"];
}

function parsePayload(value) {
  try {
    return JSON.parse(value || "{}");
  } catch {
    return {};
  }
}

function badge({ key, icon }, { state, percent = 0 }, { detail = "", queuePosition = null }) {
  const node = document.createElement("span");
  node.className = `step-badge step-${state}`;
  node.dataset.step = key;
  node.style.setProperty("--progress", String(state === "done" ? 100 : Math.round(percent)));
  node.title = tooltip(key, state, percent, detail, queuePosition);
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", node.title);
  const ring = document.createElement("span");
  ring.className = "step-ring";
  ring.setAttribute("aria-hidden", "true");
  node.append(ring);
  node.insertAdjacentHTML("beforeend", iconSvg(icon));
  return node;
}

// "Aufbereitung — 40 % · Bereinigung 2/5": the step, where it stands, and what
// it is doing — the text the status column used to carry, now on hover.
function tooltip(key, state, percent, detail, queuePosition) {
  const step = t(`fileStep.${key}`);
  if (state === "running") {
    const rounded = Math.round(percent);
    return detail
      ? t("fileStep.running", { step, percent: rounded, detail })
      : t("fileStep.runningPlain", { step, percent: rounded });
  }
  if (state === "queued") {
    return queuePosition == null
      ? t("fileStep.queued", { step })
      : t("fileStep.queuedAt", { step, pos: queuePosition });
  }
  return t(`fileStep.${state}`, { step });
}
