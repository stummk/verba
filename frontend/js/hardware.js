// Showing what this machine can carry: the memory verdict a model gets from
// the backend (services/hardware.py), rendered the same way wherever models
// are picked — settings page and setup wizard.
//
// Only local engines are rated. Whisper models, the llama.cpp catalog and the
// embedding models all run on the machine that runs Verba. An
// OpenAI-compatible endpoint does not — except when it points at this machine
// itself, and then all we can honestly offer is an estimate: which model that
// server loads, and whether it uses the GPU, is its own business.

import { currentLanguage, t } from "./i18n.js";

const FIT_LABELS = {
  ok: "models.fitOk",
  tight: "models.fitTight",
  no: "models.fitNo",
  unknown: "models.fitUnknown",
};

const gb = (mb) => Math.round((mb ?? 0) / 1024);
// one decimal, in the UI language — the backend sentences next to this one
// write "2,3 GB" in German, so a dot here would look like a different unit
const gbFine = (mb) => ((mb ?? 0) / 1024).toLocaleString(currentLanguage(), {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

// RAM and GPU of the machine the models run on — named once, so the verdicts
// next to the single models can stay short. `cpuOnly` leaves the GPU out where
// it would only mislead: the search index always encodes on the CPU.
export function hardwareLine(hw, { cpuOnly = false } = {}) {
  if (!hw) return "";
  if (cpuOnly) return t("models.hardwareRam", { ram: gb(hw.ram_total_mb) });
  const gpu = hw.gpu_name
    ? `${hw.gpu_name} (${gb(hw.vram_total_mb)} GB VRAM)`
    : t("llmModels.noGpu");
  return t("models.hardware", { ram: gb(hw.ram_total_mb), gpu });
}

// The verdict as a coloured badge, the full sentence as its tooltip.
export function fitBadge(fit) {
  if (!fit?.level) return null;
  const badge = document.createElement("span");
  badge.className = `badge badge-fit-${fit.level}`;
  badge.textContent = t(FIT_LABELS[fit.level] ?? FIT_LABELS.unknown);
  badge.title = fit.message ?? "";
  return badge;
}

// The same verdict as running text, for a hint line below a field.
export function applyFitHint(node, fit) {
  if (!node) return;
  node.textContent = fit?.message ? t("settings.modelFit", { message: fit.message }) : "";
  node.className = fit?.level ? `small fit-${fit.level}` : "small";
}

// Whether an endpoint URL points at the machine Verba itself runs on. The
// host is what decides it, so the answer is the same in the browser and on the
// server — a hostname or LAN address of that same machine is not recognised,
// and then no estimate is shown at all (better silent than wrong).
export function isLocalEndpoint(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"].includes(host);
  } catch {
    return false; // an incomplete URL is simply not local yet
  }
}

// What can realistically run on this machine — for a local endpoint Verba does
// not manage. Deliberately no badge: this is a recommendation, not a verdict.
export function endpointEstimate(hw, budget) {
  if (!hw || !budget) return "";
  const key = budget.gpu_mb > 0 ? "settings.endpointLocalGpu" : "settings.endpointLocal";
  return t(key, {
    ram: gb(hw.ram_total_mb),
    gpu: hw.gpu_name || t("llmModels.noGpu"),
    ramBudget: gbFine(budget.cpu_mb),
    vramBudget: gbFine(budget.gpu_mb),
  });
}
