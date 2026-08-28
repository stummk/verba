// First-run setup: a five-step wizard — install missing components, then
// workspace, Whisper, LLM and search configuration. Every step can be skipped
// with its defaults, and the whole wizard can be postponed at any point; only
// reaching the end marks the setup as completed (the reminder stays otherwise).

import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { fillEmbeddingSelect } from "../embeddings.js";
import { applyFitHint, endpointEstimate, isLocalEndpoint } from "../hardware.js";
import { t } from "../i18n.js";
import { fillLanguageSelect } from "../languages.js";
import { on } from "../ws.js";

const STEPS = ["install", "workspace", "whisper", "llm", "search"];

let unsubscribe = null;
let stepIndex = 0;
let settings = null;
let paths = null;

export async function render(view) {
  stepIndex = 0; // entering the wizard always starts at the beginning
  const status = await api.systemStatus();
  settings = await api.getSettings();
  paths = await api.getPaths().catch(() => null);

  view.replaceChildren(html`
    <h1>${t("setup.title")}</h1>
    <ol class="wizard-steps" id="wizard-steps"></ol>
    <div id="wizard-body"></div>
    <div class="actions wizard-actions">
      <button type="button" class="text-btn" id="wizard-back">${t("setup.back")}</button>
      <button type="button" id="wizard-next">${t("setup.next")}</button>
      <button type="button" class="text-btn" id="wizard-skip-step">${t("setup.skipStep")}</button>
      <span class="spacer"></span>
      <button type="button" class="text-btn" id="wizard-skip-all">${t("setup.later")}</button>
    </div>
  `);

  el("wizard-back").onclick = () => goTo(stepIndex - 1, status);
  el("wizard-skip-step").onclick = () => advance(status);
  el("wizard-skip-all").onclick = () => {
    location.hash = "#/";
  };
  el("wizard-next").onclick = async () => {
    const button = el("wizard-next");
    button.disabled = true;
    try {
      await saveCurrentStep();
      await advance(status);
    } catch (error) {
      toast(t("settings.saveError", { message: error.message }));
    } finally {
      button.disabled = false;
    }
  };

  await goTo(stepIndex, status);
}

function goTo(index, status) {
  stepIndex = Math.min(Math.max(index, 0), STEPS.length - 1);
  renderStepIndicator();
  el("wizard-back").hidden = stepIndex === 0;
  el("wizard-next").textContent =
    stepIndex === STEPS.length - 1 ? t("setup.finish") : t("setup.next");
  return renderStep(status);
}

async function advance(status) {
  if (stepIndex < STEPS.length - 1) {
    await goTo(stepIndex + 1, status);
    return;
  }
  // end of the wizard: the reminder disappears even if steps were skipped
  let fresh = null;
  try {
    fresh = await api.completeSetup();
    toast(t("setup.completedToast"));
  } catch (error) {
    toast(error.message);
  }
  location.hash = "#/";
  // after the hash change, so the shell brings the tabs back on the new view
  if (fresh) window.dispatchEvent(new CustomEvent("system:status", { detail: fresh }));
}

function renderStepIndicator() {
  const host = el("wizard-steps");
  host.replaceChildren(
    ...STEPS.map((step, index) => {
      const item = document.createElement("li");
      item.className = [
        "wizard-step",
        index === stepIndex ? "active" : "",
        index < stepIndex ? "done" : "",
      ]
        .filter(Boolean)
        .join(" ");
      item.append(
        Object.assign(document.createElement("span"), {
          className: "wizard-step-number",
          textContent: String(index + 1),
        }),
        Object.assign(document.createElement("span"), {
          className: "wizard-step-label",
          textContent: t(`setup.step.${step}`),
        }),
      );
      return item;
    }),
  );
}

async function renderStep(status) {
  const body = el("wizard-body");
  const step = STEPS[stepIndex];
  el("wizard-skip-step").hidden = false;
  if (step === "install") return renderInstallStep(body, status);
  if (step === "workspace") return renderWorkspaceStep(body);
  if (step === "whisper") return renderWhisperStep(body);
  if (step === "llm") return renderLlmStep(body);
  return renderSearchStep(body);
}

// ── step 1: install missing components ────────────────────────────────

function renderInstallStep(body, status) {
  body.replaceChildren(html`
    <div class="card">
      <h2>${t("setup.step.install")}</h2>
      <p class="muted">${t("setup.intro")}</p>
      <ul class="checklist" id="setup-checklist"></ul>
      <div class="actions">
        <button type="button" id="setup-run">${t("setup.start")}</button>
      </div>
    </div>
    <div class="card" id="setup-progress-card" hidden>
      <h2 id="setup-step">${t("setup.running", { step: "…", detail: "", percent: 0 })}</h2>
      <div class="progressbar"><div id="setup-bar"></div></div>
      <div class="setup-log" id="setup-log"></div>
    </div>
  `);

  renderChecklist(status.checks);
  const runButton = el("setup-run");
  const progressCard = el("setup-progress-card");

  if (status.ready) {
    runButton.disabled = true;
    runButton.textContent = t("setup.ready");
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
  }

  if (status.setup?.running) {
    runButton.disabled = true;
    progressCard.hidden = false;
    applyProgress(status.setup);
  }

  unsubscribe?.();
  unsubscribe = on("setup.progress", async (progress) => {
    if (!el("setup-progress-card")) return; // the user moved on to another step
    progressCard.hidden = false;
    applyProgress(progress);
    if (!progress.running) {
      const fresh = await api.systemStatus();
      Object.assign(status, fresh);
      renderChecklist(fresh.checks);
      if (fresh.ready && !progress.error) {
        runButton.textContent = t("setup.ready");
        toast(t("setup.installDone"));
      } else {
        runButton.disabled = false;
        runButton.textContent = t("common.retry");
      }
    }
  });
}

function renderChecklist(checks) {
  const list = el("setup-checklist");
  if (!list || !checks?.length) return;
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
    }),
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
  // the backend ticks components off while it installs them
  renderChecklist(progress.checks);
  log.textContent = (progress.log ?? []).join("\n");
  log.scrollTop = log.scrollHeight;
}

// ── step 2: workspace directory ───────────────────────────────────────

function renderWorkspaceStep(body) {
  body.replaceChildren(html`
    <div class="card">
      <h2>${t("setup.step.workspace")}</h2>
      <p class="muted">${t("setup.workspaceIntro")}</p>
      <div class="form-grid">
        <div>
          <label for="wizard-workspaces">${t("settings.workspacesDir")}</label>
          <input id="wizard-workspaces" value="${settings.general.workspaces_dir}"
                 placeholder="${paths ? paths.workspaces_default : ""}">
          <p class="hint">${t("setup.workspaceHint", {
            path: paths ? paths.workspaces_dir : "…",
          })}</p>
        </div>
      </div>
      <p class="hint">${t("setup.workspaceContents")}</p>
    </div>
  `);
}

// ── step 3: whisper ───────────────────────────────────────────────────

async function renderWhisperStep(body) {
  body.replaceChildren(html`
    <div class="card">
      <h2>${t("setup.step.whisper")}</h2>
      <p class="muted">${t("setup.whisperIntro")}</p>
      <div class="form-grid">
        <div>
          <label for="wizard-model">${t("settings.model")}</label>
          <input id="wizard-model" value="${settings.whisper.model}" list="wizard-model-list">
          <datalist id="wizard-model-list"></datalist>
          <p class="hint">${t("settings.modelHint")}</p>
          <p class="small" id="wizard-model-fit"></p>
        </div>
        <div>
          <label for="wizard-models-dir">${t("settings.modelsDir")}</label>
          <input id="wizard-models-dir" value="${settings.whisper.models_dir}"
                 placeholder="${paths ? paths.models_dir : ""}">
          <p class="hint">${t("setup.modelsDirHint")}</p>
        </div>
        <div>
          <label for="wizard-device">${t("settings.device")}</label>
          <select id="wizard-device">
            <option value="auto">${t("settings.deviceAuto")}</option>
            <option value="cuda">${t("settings.deviceCuda")}</option>
            <option value="cpu">${t("settings.deviceCpu")}</option>
          </select>
        </div>
        <div>
          <label for="wizard-language">${t("settings.language")}</label>
          <select id="wizard-language"></select>
        </div>
      </div>
    </div>
  `);

  el("wizard-device").value = settings.whisper.device;
  fillLanguageSelect(el("wizard-language"), {
    placeholder: t("settings.languageAuto"),
    selected: settings.whisper.language ?? "",
  });

  try {
    const models = await api.listModels();
    const names = [
      ...(models.builtin ?? []),
      ...(models.local ?? []).filter((name) => !(models.builtin ?? []).includes(name)),
    ];
    el("wizard-model-list")?.replaceChildren(...names.map((name) => new Option("", name)));
    bindModelFit(models);
  } catch {
    /* the model list is a convenience — the field accepts any name */
  }
}

// Says right in the wizard whether the typed model fits this machine — the
// models run here, so a 6 GB model on a 4 GB box is worth knowing before the
// first transcription rather than after it.
function bindModelFit(models) {
  const field = el("wizard-model");
  const hint = el("wizard-model-fit");
  if (!field || !hint) return;
  const show = () => applyFitHint(hint, models.models?.[field.value.trim()]);
  field.addEventListener("input", show);
  if (models.suggested && !field.value.trim()) field.value = models.suggested;
  show();
}

// ── step 4: LLM ───────────────────────────────────────────────────────

async function renderLlmStep(body) {
  body.replaceChildren(html`
    <div class="card">
      <h2>${t("setup.step.llm")}</h2>
      <p class="muted">${t("setup.llmIntro")}</p>
      <div class="segmented" role="radiogroup" id="wizard-llm-mode">
        <label><input type="radio" name="wizard-llm-mode" value="none">
          <span>${t("settings.llmOff")}</span></label>
        <label><input type="radio" name="wizard-llm-mode" value="local">
          <span>${t("settings.llmLocal")}</span></label>
        <label><input type="radio" name="wizard-llm-mode" value="openai">
          <span>${t("settings.llmEndpoint")}</span></label>
      </div>
      <div id="wizard-llm-none" hidden><p class="hint">${t("settings.llmOffHint")}</p></div>
      <div id="wizard-llm-local" hidden>
        <p class="hint">${t("setup.llmLocalHint")}</p>
        <p class="small" id="wizard-llm-fit"></p>
        <div class="form-grid">
          <div>
            <label for="wizard-llm-dir">${t("settings.llmModelsDir")}</label>
            <input id="wizard-llm-dir" value="${settings.llm.models_dir ?? ""}"
                   placeholder="${paths ? paths.llm_models_default : ""}">
            <p class="hint">${t("settings.llmModelsDirHint")}</p>
          </div>
        </div>
      </div>
      <div id="wizard-llm-openai" hidden>
        <div class="form-grid">
          <div>
            <label for="wizard-base-url">${t("settings.baseUrl")}</label>
            <input id="wizard-base-url" value="${settings.llm.base_url}"
                   placeholder="https://api.openai.com/v1">
            <p class="small" id="wizard-endpoint-estimate"></p>
          </div>
          <div>
            <label for="wizard-api-key">${t("settings.apiKey")}</label>
            <input id="wizard-api-key" type="password" value="${settings.llm.api_key}"
                   autocomplete="off">
          </div>
          <div>
            <label for="wizard-llm-model">${t("settings.llmModel")}</label>
            <input id="wizard-llm-model"
                   value="${settings.llm.mode === "openai" ? settings.llm.model : ""}"
                   placeholder="gpt-4o-mini">
          </div>
          <div>
            <label>&nbsp;</label>
            <button type="button" class="text-btn" id="wizard-llm-test">
              ${t("settings.llmTest")}
            </button>
            <p class="hint" id="wizard-llm-test-result"></p>
          </div>
        </div>
      </div>
    </div>
  `);

  const radios = [...document.querySelectorAll('input[name="wizard-llm-mode"]')];
  const show = () => {
    const mode = radios.find((r) => r.checked)?.value ?? "none";
    el("wizard-llm-none").hidden = mode !== "none";
    el("wizard-llm-local").hidden = mode !== "local";
    el("wizard-llm-openai").hidden = mode !== "openai";
  };
  (radios.find((r) => r.value === settings.llm.mode) ?? radios[0]).checked = true;
  radios.forEach((radio) => radio.addEventListener("change", show));
  show();

  el("wizard-llm-test").onclick = async () => {
    const result = el("wizard-llm-test-result");
    result.textContent = "…";
    try {
      const data = await api.llmTest(el("wizard-base-url").value.trim(), el("wizard-api-key").value);
      result.textContent = data.ok
        ? t("settings.llmTestOk", { models: data.models.slice(0, 5).join(", ") || "–" })
        : t("settings.llmTestFail", { error: data.error });
    } catch (error) {
      result.textContent = t("settings.llmTestFail", { error: error.message });
    }
  };

  // the local model runs on this machine, so name it and say whether it fits
  try {
    const status = await api.llmStatus();
    const fit = status.catalog.find((entry) => entry.name === status.recommended.name)?.fit;
    const hint = el("wizard-llm-fit");
    if (hint) {
      hint.textContent = [
        t("models.suggested", { model: status.recommended.label }),
        fit?.message ?? "",
      ].filter(Boolean).join(" · ");
      hint.className = fit ? `small fit-${fit.level}` : "small";
    }
    // an endpoint on 127.0.0.1 is this machine too — but not our program, so
    // only an estimate
    const url = el("wizard-base-url");
    const estimate = el("wizard-endpoint-estimate");
    if (url && estimate) {
      const show = () => {
        estimate.textContent = isLocalEndpoint(url.value.trim())
          ? endpointEstimate(status.hardware, status.budget)
          : "";
      };
      url.addEventListener("input", show);
      show();
    }
  } catch {
    /* without the probe the wizard simply says nothing about the hardware */
  }
}

// ── step 5: search ────────────────────────────────────────────────────

async function renderSearchStep(body) {
  body.replaceChildren(html`
    <div class="card">
      <h2>${t("setup.step.search")}</h2>
      <p class="muted">${t("setup.searchIntro")}</p>
      <div class="form-grid">
        <div>
          <label for="wizard-embedding">${t("settings.embeddingModel")}</label>
          <select id="wizard-embedding"></select>
          <p class="small" id="wizard-embedding-fit"></p>
          <p class="hint" id="wizard-embedding-hint"></p>
        </div>
        <div>
          <label for="wizard-embeddings-dir">${t("settings.embeddingsDir")}</label>
          <input id="wizard-embeddings-dir" value="${settings.search?.embeddings_dir ?? ""}"
                 placeholder="${paths ? paths.embeddings_default : ""}">
          <p class="hint">${t("settings.embeddingsDirHint")}</p>
        </div>
      </div>
      <p class="hint" id="wizard-search-note"></p>
    </div>
  `);

  try {
    const catalog = await api.searchModels();
    fillEmbeddingSelect(el("wizard-embedding"), catalog, settings.search?.embedding_model, {
      hint: el("wizard-embedding-fit"),
    });
    el("wizard-embedding-hint").textContent = t("settings.embeddingCacheHint", {
      path: catalog.cache_dir,
    });
  } catch {
    el("wizard-embedding").disabled = true;
  }

  const status = await api.searchStatus().catch(() => null);
  el("wizard-search-note").textContent = status?.available
    ? t("setup.searchReady")
    : t("setup.searchMissing");
}

// ── saving ────────────────────────────────────────────────────────────

async function saveCurrentStep() {
  const step = STEPS[stepIndex];
  if (step === "install") return; // nothing to store, the installer did that
  const payload = { ...settings };

  if (step === "workspace") {
    payload.general = { ...settings.general, workspaces_dir: el("wizard-workspaces").value.trim() };
  } else if (step === "whisper") {
    payload.whisper = {
      ...settings.whisper,
      model: el("wizard-model").value.trim(),
      models_dir: el("wizard-models-dir").value.trim(),
      device: el("wizard-device").value,
      language: el("wizard-language").value,
    };
  } else if (step === "llm") {
    const mode = [...document.querySelectorAll('input[name="wizard-llm-mode"]')].find(
      (radio) => radio.checked,
    )?.value;
    payload.llm = {
      mode: mode ?? "none",
      base_url: el("wizard-base-url")?.value.trim() ?? settings.llm.base_url,
      api_key: el("wizard-api-key")?.value ?? settings.llm.api_key,
      models_dir: el("wizard-llm-dir")?.value.trim() ?? settings.llm.models_dir,
      model: mode === "openai" ? (el("wizard-llm-model")?.value.trim() ?? "") : settings.llm.model,
    };
  } else if (step === "search") {
    payload.search = {
      ...settings.search,
      embeddings_dir: el("wizard-embeddings-dir")?.value.trim() ?? "",
    };
    const select = el("wizard-embedding");
    if (select && !select.disabled) {
      payload.search.embedding_model = select.value;
    }
  }

  const saved = await api.updateSettings(payload);
  const { workspace_move: move, reindex_started: _reindex, ...stored } = saved;
  settings = stored;
  paths = await api.getPaths().catch(() => paths);
  if (move?.projects) {
    toast(t("settings.workspaceMoveStarted", { count: move.projects }));
  }
}
