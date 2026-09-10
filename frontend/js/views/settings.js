// Settings: UI language, whisper defaults, LLM, storage/logs, model management.

import { api } from "../api.js";
import { confirmDelete } from "../confirm.js";
import { el, html, raw, toast } from "../dom.js";
import { fillEmbeddingSelect } from "../embeddings.js";
import { applyFitHint, endpointEstimate, fitBadge, hardwareLine, isLocalEndpoint } from "../hardware.js";
import { checkLine, fieldLabel, helpSlot, helpText, labelHelp, setHelp } from "../help.js";
import { iconButton, iconSvg } from "../icons.js";
import { SUPPORTED_LANGUAGES, currentLanguage, t } from "../i18n.js";
import { jobCardHost } from "../jobs.js";
import { fillLanguageSelect } from "../languages.js";
import { mountLlamaInstaller } from "../llamainstall.js";
import { viewGuard } from "../navigation.js";
import { on } from "../ws.js";

let unsubscribe = null;
let unsubscribers = [];
let llmInstallerCleanup = null;

// The sections that feed the settings payload — the others (API keys, account)
// have their own buttons, so a save button below them would save nothing.
const SAVEABLE_SECTIONS = new Set([
  "card-ui",
  "card-transcribe",
  "card-ai",
  "card-search",
  "card-storage",
  "card-system",
]);
// A changed interface language reloads the page; the confirmation has to
// survive that reload, so it waits here until the view is built again.
const SAVED_FLAG = "verba.settingsSaved";

export async function render(view) {
  if (sessionStorage.getItem(SAVED_FLAG)) {
    sessionStorage.removeItem(SAVED_FLAG);
    toast(t("settings.saved"));
  }
  const stillCurrent = viewGuard();
  let settings = await api.getSettings();
  if (!stillCurrent()) return; // the next view already owns the page
  // A normal user gets nothing to configure beyond their own account: the
  // reduced payload the backend sends has no whisper/paths/keys sections at
  // all, so the administrative form below could not even be built from it.
  if (settings.restricted) return renderPersonalSettings(view, settings);

  view.replaceChildren(html`
    <h1>${t("settings.title")}</h1>
    <div class="settings-layout" id="settings-layout">
    <nav class="settings-list" id="settings-nav">
      <button type="button" data-target="card-ui">${t("settings.ui")}</button>
      <button type="button" data-target="card-transcribe">${t("settings.navTranscribe")}</button>
      <button type="button" data-target="card-ai">${t("settings.navAi")}</button>
      <button type="button" data-target="card-search">${t("settings.searchTitle")}</button>
      <button type="button" data-target="card-api">${t("settings.apiTitle")}</button>
      <button type="button" data-target="card-storage">${t("settings.storage")}</button>
      <button type="button" data-target="card-system">${t("settings.system")}</button>
      <button type="button" data-target="card-account">${t("settings.accountTitle")}</button>
    </nav>
    <form class="settings" id="settings-form">
      <button type="button" class="text-btn settings-back" id="settings-back">
        ‹ ${t("settings.backToList")}
      </button>
      <div class="card" id="card-ui">
        <h2>${t("settings.ui")}</h2>
        <div class="form-grid">
          <div class="setting-field">
            <label for="ui-language">${t("settings.uiLanguage")}</label>
            <select id="ui-language"></select>
          </div>
          <div class="setting-field">
            <label>${t("docs.title")}</label>
            <a class="btn icon-btn" href="#/docs"
               title="${t("settings.docsOpen")}" aria-label="${t("settings.docsOpen")}"
               >${raw(iconSvg("article"))}</a>
          </div>
        </div>
      </div>

      <div class="card" id="card-transcribe">
        <h2>${t("settings.whisper")}</h2>
        <div class="form-grid">
          <div class="setting-field">
            ${raw(fieldLabel("whisper-model", t("settings.model"), t("settings.modelHint")))}
            <input id="whisper-model" value="${settings.whisper.model}" list="model-list">
            <datalist id="model-list"></datalist>
          </div>
          <div class="setting-field">
            <label for="whisper-models-dir">${t("settings.modelsDir")}</label>
            <input id="whisper-models-dir" value="${settings.whisper.models_dir}"
                   placeholder="${t("settings.modelsDirPlaceholder")}">
          </div>
          <div class="setting-field">
            <label for="whisper-device">${t("settings.device")}</label>
            <select id="whisper-device">
              <option value="auto">${t("settings.deviceAuto")}</option>
              <option value="cuda">${t("settings.deviceCuda")}</option>
              <option value="cpu">${t("settings.deviceCpu")}</option>
            </select>
          </div>
          <div class="setting-field">
            <label for="whisper-compute">${t("settings.compute")}</label>
            <select id="whisper-compute">
              <option value="auto">${t("settings.computeAuto")}</option>
              <option>int8</option><option>int8_float16</option>
              <option>float16</option><option>float32</option>
            </select>
          </div>
          <div class="setting-field">
            <label for="whisper-language">${t("settings.language")}</label>
            <select id="whisper-language"></select>
          </div>
        </div>
        ${raw(labelHelp(
          `<h3 class="subhead">${t("models.title")}</h3>`,
          helpText(t("models.intro"), t("models.fitHint")), "head-row sub"
        ))}
        <p class="small" id="whisper-hardware"></p>
        <div id="model-list-host"></div>
      </div>

      <div class="card" id="card-ai">
        ${raw(labelHelp(`<h2>${t("settings.navAi")}</h2>`, t("settings.aiIntro"), "head-row"))}
        <div class="segmented" role="radiogroup" id="llm-mode-group">
          <label><input type="radio" name="llm-mode" value="none">
            <span>${t("settings.llmOff")}</span></label>
          <label><input type="radio" name="llm-mode" value="local">
            <span>${t("settings.llmLocal")}</span></label>
          <label><input type="radio" name="llm-mode" value="openai">
            <span>${t("settings.llmEndpoint")}</span></label>
        </div>

        <div id="llm-none-section" hidden>
          <p class="hint">${t("settings.llmOffHint")}</p>
        </div>

        <div class="form-grid" id="llm-reasoning-field" hidden>
          <div class="setting-field">
            ${raw(fieldLabel("llm-reasoning", t("settings.reasoning"), t("settings.reasoningHint")))}
            <select id="llm-reasoning">
              <option value="off">${t("settings.reasoningOff")}</option>
              <option value="low">${t("settings.reasoningLow")}</option>
              <option value="auto">${t("settings.reasoningAuto")}</option>
            </select>
          </div>
        </div>

        <div id="llm-local-section" hidden>
          <p class="small" id="llm-hardware"></p>
          <div id="llm-binary"></div>
          <div id="llm-catalog"></div>
          <div class="form-grid">
            <div class="setting-field">
              ${raw(fieldLabel(
                "llm-local-model", t("settings.llmLocalModel"), t("settings.llmLocalModelHint")
              ))}
              <select id="llm-local-model"></select>
            </div>
            <div class="setting-field">
              ${raw(fieldLabel(
                "llm-models-dir", t("settings.llmModelsDir"), t("settings.llmModelsDirHint")
              ))}
              <input id="llm-models-dir" value="${settings.llm.models_dir ?? ""}">
              <p class="hint" id="llm-models-dir-current"></p>
            </div>
          </div>
        </div>

        <div id="llm-openai-section" hidden>
          <div class="form-grid">
            <div class="setting-field">
              <label for="llm-base-url">${t("settings.baseUrl")}</label>
              <input id="llm-base-url" value="${settings.llm.base_url}"
                     placeholder="https://api.openai.com/v1">
              <p class="small" id="llm-endpoint-estimate"></p>
            </div>
            <div class="setting-field">
              ${raw(fieldLabel("llm-api-key", t("settings.apiKey"), t("settings.apiKeyHint")))}
              <input id="llm-api-key" type="password" value="${settings.llm.api_key}" autocomplete="off">
            </div>
            <div class="setting-field">
              <label for="llm-model">${t("settings.llmModel")}</label>
              <input id="llm-model" value="${settings.llm.mode === "openai" ? settings.llm.model : ""}"
                     placeholder="gpt-4o-mini">
            </div>
            <div class="setting-action">
              <button type="button" class="text-btn" id="llm-test">${t("settings.llmTest")}</button>
              <p class="hint" id="llm-test-result"></p>
            </div>
          </div>
        </div>
      </div>

      <div class="card" id="card-search">
        ${raw(labelHelp(
          `<h2>${t("settings.searchTitle")}</h2>`, t("settings.searchIntro"), "head-row"
        ))}
        <p class="small" id="search-status"></p>
        <p class="small" id="search-hardware"></p>
        <div id="search-jobs" hidden></div>
        <div class="form-grid">
          <div class="setting-field">
            ${raw(fieldLabel(
              "search-embedding-model", t("settings.embeddingModel"),
              t("settings.embeddingModelHint")
            ))}
            <select id="search-embedding-model"></select>
            <p class="small" id="search-embedding-fit"></p>
          </div>
          <div class="setting-field">
            ${raw(fieldLabel(
              "search-embeddings-dir", t("settings.embeddingsDir"), t("settings.embeddingsDirHint")
            ))}
            <input id="search-embeddings-dir"
                   value="${settings.search?.embeddings_dir ?? ""}">
            <p class="hint" id="search-embedding-cache"></p>
            <p class="error" id="search-embedding-error" hidden></p>
          </div>
          <div class="setting-action">
            ${raw(labelHelp(
              `<button type="button" class="text-btn" id="search-reindex"
                >${t("settings.reindex")}</button>`,
              t("settings.reindexHint"), "inline-row"
            ))}
          </div>
        </div>
      </div>

      <div class="card" id="card-api">
        ${raw(labelHelp(
          `<h2>${t("settings.apiTitle")}</h2>`,
          helpText(t("settings.apiIntro"), t("settings.apiDocsHint")), "head-row"
        ))}
        <p class="small"><code>${location.origin}/v1/audio/transcriptions</code></p>
        <div id="apikey-new" class="apikey-new" hidden></div>
        <div id="apikey-list"></div>
        <div class="form-grid">
          <div class="setting-field">
            <label for="apikey-name">
              ${t("settings.apiKeyName")}<span class="required-mark" aria-hidden="true">*</span>
            </label>
            <div class="inline-form">
              <input id="apikey-name" maxlength="100" autocomplete="off"
                     aria-required="true">
              <button type="button" class="icon-btn" id="apikey-create" disabled
                      title="${t("settings.apiKeyCreate")}"
                      aria-label="${t("settings.apiKeyCreate")}"
                      >${raw(iconSvg("add"))}</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card" id="card-storage">
        <h2>${t("settings.storage")}</h2>
        <div id="storage-jobs" hidden></div>
        <div class="form-grid">
          <div class="setting-field">
            ${raw(fieldLabel(
              "general-data-dir", t("settings.dataDir"),
              helpText(t("settings.dataDirHint"), t("settings.dataDirRestartHint"))
            ))}
            <input id="general-data-dir" value="${settings.general.data_dir}">
            <p class="hint" id="data-dir-hint"></p>
            <p class="warning-box" id="data-dir-pending" hidden></p>
          </div>
          <div class="setting-field">
            ${raw(fieldLabel(
              "general-workspaces", t("settings.workspacesDir"), t("settings.workspacesMoveHint")
            ))}
            <input id="general-workspaces" value="${settings.general.workspaces_dir}"
                   placeholder="${t("settings.workspacesDirPlaceholder")}">
            <p class="hint" id="workspaces-hint"></p>
          </div>
            ${raw(checkLine(
              "general-audio-backup", t("settings.audioBackup"), t("settings.audioBackupHint")
            ))}
          <div class="setting-field">
            ${raw(fieldLabel("server-port", t("settings.port"), t("settings.portHint")))}
            <input id="server-port" type="number" min="1" max="65535" value="${settings.server.port}">
          </div>
          <div class="setting-field">
            <label for="log-level">${t("settings.logLevel")}</label>
            <select id="log-level">
              <option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option>
            </select>
          </div>
          <div class="setting-field">
            ${raw(fieldLabel(
              "log-retention", t("settings.logRetention"), t("settings.logRetentionHint")
            ))}
            <input id="log-retention" type="number" min="1" max="365"
                   value="${settings.logging.retention_days}">
          </div>
        </div>
      </div>

      <div class="card" id="card-system">
        ${raw(labelHelp(`<h2>${t("settings.system")}</h2>`, t("system.intro"), "head-row"))}
        <div class="model-row" id="update-row">
          <span class="model-name" id="update-current">Verba</span>
          ${raw(helpSlot("update-help"))}
          <span class="spacer"></span>
          <button type="button" class="icon-btn" id="update-check"
                  title="${t("update.check")}" aria-label="${t("update.check")}"
                  >${raw(iconSvg("refresh"))}</button>
          <button type="button" class="icon-btn" id="update-install" disabled
                  title="${t("update.upToDate")}" aria-label="${t("update.upToDate")}"
                  >${raw(iconSvg("download"))}</button>
        </div>
        <div class="setup-log" id="update-notes" hidden></div>
        <div class="progressbar" id="update-bar" hidden><div></div></div>
        <p class="small muted" id="update-log-title" hidden>${t("update.logTitle")}</p>
        <div class="setup-log" id="update-log" hidden></div>
        ${raw(checkLine("update-auto", t("settings.updateCheck"), t("settings.updateCheckHint")))}
        <div id="os-section" hidden>
          <div class="model-row" id="os-row">
            <span class="model-name">${t("osUpdate.title")}</span>
            ${raw(helpSlot("os-help"))}
            <span class="spacer"></span>
            <button type="button" class="icon-btn" id="os-run" disabled
                    title="${t("osUpdate.run")}" aria-label="${t("osUpdate.run")}"
                    >${raw(iconSvg("upgrade"))}</button>
          </div>
          ${raw(checkLine("os-full", t("osUpdate.full"), t("osUpdate.fullHint")))}
          <p class="small muted" id="os-log-title" hidden>${t("osUpdate.logTitle")}</p>
          <div class="setup-log" id="os-log" hidden></div>
        </div>
        <div id="cuda-section" hidden>
          <div class="model-row" id="cuda-row">
            <span class="model-name">${t("cuda.title")}</span>
            ${raw(helpSlot("cuda-help"))}
            <span class="spacer"></span>
            <button type="button" class="icon-btn" id="cuda-install" hidden
                    title="${t("cuda.install")}" aria-label="${t("cuda.install")}"
                    >${raw(iconSvg("download"))}</button>
          </div>
          <p class="small muted" id="cuda-log-title" hidden>${t("cuda.logTitle")}</p>
          <div class="setup-log" id="cuda-log" hidden></div>
        </div>
        ${raw(labelHelp(
          `<h3 class="subhead">${t("monitor.title")}</h3>`, t("monitor.intro"), "head-row sub"
        ))}
        <div class="resource-bars" id="resource-bars"></div>
        <dl class="info-list" id="system-info"></dl>
      </div>

      <div class="card" id="card-account">
        ${raw(labelHelp(
          `<h2>${t("settings.accountTitle")}</h2>`, t("settings.accountIntro"), "head-row"
        ))}
        <div id="account-host"></div>
        ${raw(labelHelp(
          `<h3 class="subhead">${t("users.title")}</h3>`,
          t("settings.usersIntro"), "head-row sub"
        ))}
        <a class="btn tonal" href="#/users">${t("settings.usersOpen")}</a>
      </div>

      <div class="actions" id="settings-actions">
        <button type="submit" id="settings-save" disabled>${t("common.save")}</button>
      </div>
    </form>
    </div>
  `);
  mountAccount(el("account-host"));

  // populate selects
  const uiLanguageSelect = el("ui-language");
  for (const code of SUPPORTED_LANGUAGES) {
    uiLanguageSelect.append(new Option(t(`lang.${code}`), code));
  }
  uiLanguageSelect.value = settings.general.ui_language || currentLanguage();

  fillLanguageSelect(el("whisper-language"), {
    placeholder: t("settings.languageAuto"),
    selected: settings.whisper.language ?? "",
  });

  el("llm-reasoning").value = settings.llm.reasoning ?? "off";
  el("whisper-device").value = settings.whisper.device;
  el("whisper-compute").value = settings.whisper.compute_type;
  el("log-level").value = settings.logging.level;

  // AI mode: exclusive choice — only the selected path's fields are visible
  const modeRadios = [...document.querySelectorAll('input[name="llm-mode"]')];
  const currentMode = () => modeRadios.find((r) => r.checked)?.value ?? "none";
  function showLlmSection() {
    const mode = currentMode();
    el("llm-none-section").hidden = mode !== "none";
    el("llm-local-section").hidden = mode !== "local";
    el("llm-openai-section").hidden = mode !== "openai";
    // the reasoning control applies to both paths, so it hangs off "is there
    // an LLM at all" rather than off one of them
    el("llm-reasoning-field").hidden = mode === "none";
  }
  (modeRadios.find((r) => r.value === settings.llm.mode) ?? modeRadios[0]).checked = true;
  modeRadios.forEach((radio) => radio.addEventListener("change", showLlmSection));
  showLlmSection();

  // master-detail sections: list of sections, one section shown at a time
  // (mobile: list ↔ detail with back button, desktop: sidebar + detail)
  const layout = el("settings-layout");
  const cards = [...document.querySelectorAll("#settings-form .card")];
  const sectionButtons = [...el("settings-nav").querySelectorAll("button")];
  function selectSection(id, openDetail) {
    for (const card of cards) card.hidden = card.id !== id;
    // the button belongs to the sections that hold settings — and stays with
    // an unsaved change, so switching sections cannot strand it
    el("settings-actions").hidden =
      !SAVEABLE_SECTIONS.has(id) && el("settings-save").disabled;
    for (const button of sectionButtons) {
      button.classList.toggle("selected", button.dataset.target === id);
    }
    if (openDetail) layout.classList.add("show-detail");
    window.scrollTo({ top: 0 });
  }
  for (const button of sectionButtons) {
    button.onclick = () => selectSection(button.dataset.target, true);
  }
  el("settings-back").onclick = () => {
    layout.classList.remove("show-detail");
    window.scrollTo({ top: 0 });
  };
  selectSection("card-ui", false);

  el("llm-local-model").dataset.value = settings.llm.mode === "local" ? settings.llm.model : "";
  el("search-reindex").onclick = async () => {
    try {
      await api.searchReindex();
      toast(t("settings.reindexStarted"));
    } catch (error) {
      toast(error.message);
    }
  };

  // A key without a label cannot be told apart later — the button stays
  // disabled until the field holds something. That is also why the input
  // carries no `required`: it sits in the settings form, where an empty
  // required field would block saving from every section.
  const apiKeyName = el("apikey-name");
  const apiKeyCreate = el("apikey-create");
  apiKeyName.oninput = () => {
    apiKeyCreate.disabled = !apiKeyName.value.trim();
  };

  apiKeyCreate.onclick = async () => {
    const name = apiKeyName.value.trim();
    if (!name) return;
    try {
      const created = await api.createApiKey(name);
      apiKeyName.value = "";
      apiKeyCreate.disabled = true;
      showNewApiKey(created.key);
      await refreshApiKeys();
    } catch (error) {
      toast(error.message);
    }
  };

  el("update-auto").checked = settings.updates?.check_enabled ?? true;
  el("general-audio-backup").checked = settings.general?.audio_backup ?? true;
  el("update-check").onclick = async () => {
    setHelp(el("update-help"), t("update.checking"));
    await refreshUpdate(true);
  };
  el("update-install").onclick = async () => {
    const button = el("update-install");
    button.disabled = true;
    button.classList.remove("filled");
    try {
      const result = await api.startUpdate();
      // a refused start never reaches the event stream — say so right here
      if (!result.started) {
        toast(result.reason);
        button.disabled = false;
        button.classList.add("filled");
      }
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      button.classList.add("filled");
    }
  };

  // an icon-only button has to say which of the two runs it starts
  el("os-full").onchange = () => nameOsAction();
  el("os-run").onclick = async () => {
    const button = el("os-run");
    button.disabled = true;
    button.classList.remove("filled");
    // the choice for this run has been sent — it must not look changeable
    el("os-full").disabled = true;
    setHelp(el("os-help"), t("osUpdate.running"));
    try {
      const result = await api.startOsUpdate(el("os-full").checked);
      if (!result.started) {
        toast(result.reason);
        await refreshOsUpdate();
      }
    } catch (error) {
      toast(error.message);
      await refreshOsUpdate();
    }
  };

  // the GPU libraries: the one component an installation usually acquires
  // after the first run, so it gets its own button instead of sending the
  // administrator back through the wizard
  el("cuda-install").onclick = async () => {
    const button = el("cuda-install");
    button.disabled = true;
    setHelp(el("cuda-help"), t("cuda.running"));
    el("cuda-log-title").hidden = false;
    el("cuda-log").hidden = false;
    try {
      const result = await api.installCudaLibs();
      if (!result.started) {
        toast(result.reason);
        await refreshSystemInfo();
      }
    } catch (error) {
      toast(error.message);
      await refreshSystemInfo();
    }
  };

  // reindex and workspace move run in the background — show them where they
  // were started instead of only in the top bar
  const searchJobs = jobCardHost(el("search-jobs"), {
    filter: (job) => job.kind === "reindex_search",
    onCancel: (job) => api.cancelJob(job.id).catch((error) => toast(error.message)),
  });
  const storageJobs = jobCardHost(el("storage-jobs"), {
    filter: (job) => job.kind === "move_workspace",
    onCancel: (job) => api.cancelJob(job.id).catch((error) => toast(error.message)),
  });
  for (const job of await api.listJobs(true).catch(() => [])) {
    searchJobs.apply(job);
    storageJobs.apply(job);
  }

  unsubscribers.forEach((off) => off());
  unsubscribers = [
    on("job.update", async (job) => {
      searchJobs.apply(job);
      storageJobs.apply(job);
      if (job.kind === "reindex_search" && job.status === "done") await refreshSearchStatus();
      if (job.kind === "move_workspace" && job.status === "done") await refreshPaths();
    }),
    // the app updates itself: download, installation and — on the desktop —
    // the restart, all reported as one growing log
    on("update.progress", async (info) => {
      if (!el("update-row")) return; // the view moved on
      showUpdateProgress(info);
      if (info.state === "error") toast(t("update.failed", { detail: info.detail }));
      if (info.state !== "running") await refreshUpdate();
    }),
    // installing the CUDA libraries is a pip run like the setup's own: the
    // same progress events, shown next to the button that started it
    on("setup.progress", async (info) => {
      const log = el("cuda-log");
      if (!log) return; // the view moved on
      log.textContent = (info.log ?? []).join("\n");
      log.scrollTop = log.scrollHeight;
      if (info.running) return;
      if (info.error) toast(t("cuda.failed", { detail: info.error }));
      await refreshSystemInfo();
    }),
    // the server updates its own packages: apt says what it does, line by line
    on("system.upgrade", async (run) => {
      if (!el("os-row")) return; // the view moved on
      showOsProgress(run);
      if (run.state === "error") toast(t("osUpdate.failed", { detail: run.detail }));
      if (run.state !== "running") await refreshOsUpdate();
    }),
  ];

  unsubscribe?.();
  unsubscribe = on("model.download", async (info) => {
    if (info.state === "done") toast(t("models.downloadDone", { name: info.name }));
    if (info.state === "error") {
      toast(t("models.downloadError", { name: info.name, detail: info.detail }));
    }
    if (info.scope === "llm" || info.scope === "llm-binary") {
      updateLlmDownloadProgress(info);
      if (info.state !== "running") await refreshLlmSection();
    } else {
      await refreshModels();
    }
  });

  el("llm-test").onclick = async () => {
    const result = el("llm-test-result");
    result.textContent = "…";
    try {
      const data = await api.llmTest(el("llm-base-url").value.trim(), el("llm-api-key").value);
      result.textContent = data.ok
        ? t("settings.llmTestOk", { models: data.models.slice(0, 5).join(", ") || "–" })
        : t("settings.llmTestFail", { error: data.error });
    } catch (error) {
      result.textContent = t("settings.llmTestFail", { error: error.message });
    }
  };

  // ── save only what changed ──────────────────────────────────────────
  // The button stays disabled until a field differs from what the backend
  // sent, so "Save" always means there is something to save.
  function collectPayload() {
    return {
      ...settings,
      whisper: {
        model: el("whisper-model").value.trim(),
        models_dir: el("whisper-models-dir").value.trim(),
        device: el("whisper-device").value,
        compute_type: el("whisper-compute").value,
        language: el("whisper-language").value,
      },
      llm: {
        mode: currentMode(),
        base_url: el("llm-base-url").value.trim(),
        api_key: el("llm-api-key").value,
        models_dir: el("llm-models-dir").value.trim(),
        reasoning: el("llm-reasoning").value,
        model:
          currentMode() === "local"
            ? el("llm-local-model").value
            : el("llm-model").value.trim(),
      },
      search: {
        embedding_model: el("search-embedding-model").value,
        embeddings_dir: el("search-embeddings-dir").value.trim(),
      },
      general: {
        ...settings.general,
        ui_language: el("ui-language").value,
        data_dir: el("general-data-dir").value.trim(),
        workspaces_dir: el("general-workspaces").value.trim(),
        audio_backup: el("general-audio-backup").checked,
      },
      updates: { check_enabled: el("update-auto").checked },
      server: { port: Number(el("server-port").value) },
      logging: {
        level: el("log-level").value,
        retention_days: Number(el("log-retention").value),
      },
    };
  }

  const saveButton = el("settings-save");
  let pristine = JSON.stringify(collectPayload());
  let touched = false;
  function refreshDirty() {
    saveButton.disabled = JSON.stringify(collectPayload()) === pristine;
  }
  function markPristine() {
    pristine = JSON.stringify(collectPayload());
    saveButton.disabled = true;
  }
  const form = el("settings-form");
  const onEdit = () => {
    touched = true;
    refreshDirty();
  };
  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);

  form.onsubmit = async (event) => {
    event.preventDefault();
    saveButton.disabled = true; // the run is under way — nothing to press again
    const previousLanguage = settings.general.ui_language || "de";
    const payload = collectPayload();
    try {
      const saved = await api.updateSettings(payload);
      if (payload.general.ui_language !== previousLanguage) {
        sessionStorage.setItem(SAVED_FLAG, "1"); // confirmed after the reload
        location.reload(); // reload with the new catalog
        return;
      }
      toast(t("settings.saved"));
      // both are background jobs — say so instead of leaving the user guessing
      if (saved.workspace_move?.projects) {
        toast(t("settings.workspaceMoveStarted", { count: saved.workspace_move.projects }));
      }
      if (saved.reindex_started) toast(t("settings.reindexStarted"));
      // the data directory is the one change that waits for a restart: say so
      // here, and refresh the shell's status so the reminder on the start page
      // appears right away instead of only after the next page load
      if (saved.data_move) {
        toast(t("settings.dataDirStaged"));
        const status = await api.systemStatus().catch(() => null);
        if (status) window.dispatchEvent(new CustomEvent("system:status", { detail: status }));
      }
      // keep the settings only: the side-effect flags are not part of them
      const {
        workspace_move: _move,
        data_move: _dataMove,
        reindex_started: _reindex,
        ...stored
      } = saved;
      settings = stored;
      // the save is through — a section that fails to redraw itself is not a
      // failed save and must not be reported as one
      await Promise.allSettled([
        refreshPaths(),
        refreshSearchStatus(),
        refreshEmbeddingModels(settings.search?.embedding_model),
        refreshLlmSection(),
      ]);
      touched = false;
      markPristine(); // what is on screen is what the backend now holds
    } catch (error) {
      toast(t("settings.saveError", { message: error.message }));
      refreshDirty(); // the change is still there and still wants saving
    }
  };

  // Loaded last, and none of it can take the form with it: the sections only
  // decorate the page (model lists, hardware lines, index status), while
  // saving is what the page is for. A single rejected loader used to abort
  // the render before the submit handler existed — every section's save then
  // did nothing at all, without a hint anywhere.
  const sections = await Promise.allSettled([
    refreshModels(),
    refreshLlmSection(),
    refreshSystemInfo(),
    refreshUpdate(),
    refreshOsUpdate(),
    refreshEmbeddingModels(settings.search?.embedding_model),
    refreshSearchStatus(),
    refreshPaths(),
    refreshApiKeys(),
  ]);
  for (const section of sections) {
    if (section.status === "rejected") console.error("settings section failed:", section.reason);
  }
  if (!stillCurrent()) return;
  // the loaders filled selects — that is the baseline, unless the user was
  // quicker than they were
  if (!touched) markPristine();
  startResourceMonitor();
}

/**
 * Everything a normal user may change: their language, the guide, their own
 * account. No sidebar — there is not enough here to navigate between.
 */
async function renderPersonalSettings(view, settings) {
  view.replaceChildren(html`
    <h1>${t("settings.title")}</h1>
    <form class="settings" id="settings-form">
      <div class="card">
        <h2>${t("settings.ui")}</h2>
        <div class="form-grid">
          <div class="setting-field">
            <label for="ui-language">${t("settings.uiLanguage")}</label>
            <select id="ui-language"></select>
          </div>
          <div class="setting-field">
            <label>${t("docs.title")}</label>
            <a class="btn icon-btn" href="#/docs"
               title="${t("settings.docsOpen")}" aria-label="${t("settings.docsOpen")}"
               >${raw(iconSvg("article"))}</a>
          </div>
        </div>
        <div class="actions">
          <button type="submit" id="settings-save" disabled>${t("common.save")}</button>
        </div>
      </div>
      <div class="card">
        ${raw(labelHelp(
          `<h2>${t("settings.accountTitle")}</h2>`, t("settings.accountIntro"), "head-row"
        ))}
        <div id="account-host"></div>
      </div>
    </form>
  `);

  const uiLanguageSelect = el("ui-language");
  for (const code of SUPPORTED_LANGUAGES) {
    uiLanguageSelect.append(new Option(t(`lang.${code}`), code));
  }
  uiLanguageSelect.value = settings.general.ui_language || currentLanguage();

  // nothing to save until the one field a normal user owns actually changes
  const chosenLanguage = uiLanguageSelect.value;
  uiLanguageSelect.onchange = () => {
    el("settings-save").disabled = uiLanguageSelect.value === chosenLanguage;
  };

  el("settings-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      // the payload keeps the shape the backend sent; only the one field a
      // normal user owns is changed
      await api.updateSettings({
        ...settings,
        general: { ...settings.general, ui_language: uiLanguageSelect.value },
      });
      sessionStorage.setItem(SAVED_FLAG, "1"); // confirmed after the reload
      location.reload(); // the whole interface changes language
    } catch (error) {
      toast(error.message);
    }
  };

  mountAccount(el("account-host"));
}

/** Change own password, delete own account — identical for both variants. */
function mountAccount(host) {
  host.replaceChildren(html`
    <div class="form-grid">
      <div class="setting-field">
        <label for="account-current">${t("login.currentPassword")}</label>
        <input id="account-current" type="password" autocomplete="current-password">
      </div>
      <div class="setting-field">
        <label for="account-new">${t("login.newPassword")}</label>
        <input id="account-new" type="password" autocomplete="new-password">
      </div>
    </div>
    <div class="actions">
      <button type="button" class="btn" id="account-change">${t("login.changeSubmit")}</button>
    </div>
    <h3 class="subhead">${t("settings.deleteAccount")}</h3>
    <p class="hint">${t("settings.deleteAccountHint")}</p>
    <div class="actions">
      <button type="button" class="text-btn danger" id="account-delete">
        ${t("settings.deleteAccount")}
      </button>
    </div>
  `);

  el("account-change").onclick = async () => {
    try {
      await api.changePassword(el("account-current").value, el("account-new").value);
      el("account-current").value = "";
      el("account-new").value = "";
      toast(t("settings.passwordChanged"));
    } catch (error) {
      toast(error.message);
    }
  };

  el("account-delete").onclick = async () => {
    const ok = await confirmDelete({
      title: t("settings.deleteAccount"),
      message: t("settings.deleteAccountConfirm"),
    });
    if (!ok) return;
    const password = prompt(t("login.currentPassword"));
    if (!password) return;
    try {
      await api.deleteOwnAccount(password);
      location.reload();
    } catch (error) {
      toast(error.message);
    }
  };
}


async function refreshModels() {
  let models;
  try {
    models = await api.listModels();
  } catch {
    return;
  }

  // exactly one list: built-in models (installed or downloadable) plus any
  // custom model folders found below the models directory
  const local = models.local ?? [];
  const builtin = models.builtin ?? [];
  const names = [...builtin, ...local.filter((name) => !builtin.includes(name))];

  const datalist = el("model-list");
  if (datalist) {
    datalist.replaceChildren(...names.map((name) => new Option("", name)));
  }

  const hardware = el("whisper-hardware");
  if (hardware && models.hardware) {
    hardware.textContent = [
      hardwareLine(models.hardware),
      models.suggested ? t("models.suggested", { model: models.suggested }) : "",
    ].filter(Boolean).join(" · ");
  }

  const host = el("model-list-host");
  if (host) {
    const installed = new Set([...(models.installed ?? []), ...local]);
    const downloading = new Set(models.downloading ?? []);
    host.replaceChildren(...names.map((name) => modelRow(name, {
      installed: installed.has(name),
      downloading: downloading.has(name),
      custom: !builtin.includes(name),
      fit: models.models?.[name],
    })));
  }
}

function modelRow(name, { installed, downloading, custom, fit }) {
  const row = document.createElement("div");
  row.className = "model-row";
  row.append(Object.assign(document.createElement("span"), {
    className: "model-name", textContent: custom ? `📁 ${name}` : name,
  }));
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  row.append(spacer);

  const fitting = fitBadge(fit);
  if (fitting) row.append(fitting);

  if (downloading) {
    row.append(Object.assign(document.createElement("span"), {
      className: "muted small", textContent: t("models.downloading"),
    }));
  } else if (installed) {
    row.append(Object.assign(document.createElement("span"), {
      className: "badge badge-done", textContent: t("models.installed"),
    }));
    row.append(iconButton("delete", t("common.delete"), async () => {
      if (!await confirmDelete({ message: t("models.deleteConfirm", { name }) })) return;
      try {
        await api.deleteModel(name);
        toast(t("models.deleted"));
        await refreshModels();
      } catch (error) {
        toast(error.message);
      }
    }));
  } else {
    row.append(iconButton("download", t("models.download"), async () => {
      try {
        await api.downloadModel(name);
        toast(t("models.downloadStarted", { name }));
        // no manual refresh — the model.download "running" event triggers it
      } catch (error) {
        toast(error.message);
      }
    }));
  }
  return row;
}

// ── local LLM (llama.cpp) management ──────────────────────────────────

async function refreshLlmSection() {
  let status;
  try {
    status = await api.llmStatus();
  } catch {
    return;
  }

  const hardware = el("llm-hardware");
  if (hardware) {
    hardware.textContent = `${hardwareLine(status.hardware)} · ${
      t("models.suggested", { model: status.recommended.label })}`;
  }

  bindEndpointEstimate(status);

  const binaryHost = el("llm-binary");
  if (binaryHost) {
    // the installer with its live log — the same component the wizard mounts
    llmInstallerCleanup?.();
    llmInstallerCleanup = mountLlamaInstaller(binaryHost, {
      status,
      // the wizard sets up; taking it apart again belongs here
      withUninstall: true,
      confirmUninstall: () => confirmDelete({ message: t("llmModels.uninstallConfirm") }),
      extraControls: (current) => (current?.server_running
        ? [iconButton("stop", t("llmModels.stopServer"), async () => {
            await api.llmStopServer().catch((e) => toast(e.message));
            await refreshLlmSection();
          })]
        : []),
    });
  }

  const catalogHost = el("llm-catalog");
  if (catalogHost) {
    const installedFiles = new Set(status.installed.map((m) => m.file));
    const catalogFiles = new Set(status.catalog.map((entry) => entry.file));
    catalogHost.replaceChildren(...status.catalog.map((entry) => {
      const row = document.createElement("div");
      row.className = "model-row";
      const name = document.createElement("span");
      name.className = "model-name";
      name.textContent = entry.label;
      if (entry.name === status.recommended.name) {
        name.textContent += " ★";
        name.title = t("llmModels.recommended");
      }
      row.append(name, Object.assign(document.createElement("span"), { className: "spacer" }));
      const fitting = fitBadge(entry.fit);
      if (fitting) row.append(fitting);
      if (installedFiles.has(entry.file)) {
        row.append(Object.assign(document.createElement("span"), {
          className: "badge badge-done", textContent: t("models.installed"),
        }));
        row.append(deleteGgufButton(entry.file, entry.label));
      } else {
        const size = `${(entry.size_mb / 1024).toFixed(1)} GB`;
        row.append(iconButton("download", `${t("models.download")} (${size})`, async () => {
          try {
            await api.llmDownload(entry.name);
            toast(t("models.downloadStarted", { name: entry.name }));
          } catch (error) {
            toast(error.message);
          }
        }));
      }
      const progress = document.createElement("div");
      progress.className = "small muted";
      progress.dataset.llmProgress = entry.name;
      const wrap = document.createElement("div");
      wrap.append(row, progress);
      return wrap;
    }));
    // A GGUF that is not from the catalog — put there by hand, or left over
    // from an older catalog — was usable but invisible: no row, so no way to
    // get rid of the gigabytes it occupies.
    for (const model of status.installed) {
      if (catalogFiles.has(model.file)) continue;
      const row = document.createElement("div");
      row.className = "model-row";
      row.append(
        Object.assign(document.createElement("span"), {
          className: "model-name", textContent: model.file,
        }),
        Object.assign(document.createElement("span"), { className: "spacer" }),
        Object.assign(document.createElement("span"), {
          className: "badge badge-pending",
          textContent: `${(model.size_mb / 1024).toFixed(1)} GB`,
        }),
      );
      const fitting = fitBadge(model.fit);
      if (fitting) row.append(fitting);
      row.append(deleteGgufButton(model.file, model.file));
      catalogHost.append(row);
    }
  }

  const modelSelect = el("llm-local-model");
  if (modelSelect) {
    const previous = modelSelect.dataset.value || modelSelect.value;
    modelSelect.replaceChildren(new Option(t("settings.llmLocalAuto"), ""));
    for (const model of status.installed) {
      modelSelect.append(new Option(model.file, model.file));
    }
    modelSelect.value = [...modelSelect.options].some((o) => o.value === previous)
      ? previous
      : "";
    modelSelect.dataset.value = modelSelect.value;
    modelSelect.onchange = () => { modelSelect.dataset.value = modelSelect.value; };
  }
}

/**
 * Delete one GGUF file: the same flow for a catalog model and for one that
 * was put into the models directory by hand. `label` is only what the
 * confirmation calls it — the file name is what gets deleted.
 */
function deleteGgufButton(file, label) {
  return iconButton("delete", t("common.delete"), async () => {
    const ok = await confirmDelete({ message: t("models.deleteConfirm", { name: label }) });
    if (!ok) return;
    await api.llmDeleteModel(file).catch((error) => toast(error.message));
    toast(t("models.deleted"));
    await refreshLlmSection();
  });
}

// An endpoint on 127.0.0.1 runs on this machine, so its memory is ours — but
// the server is somebody else's program: what it loads and whether it uses the
// GPU is unknown here. Hence an estimate that appears only for a local URL,
// and no verdict badge.
function bindEndpointEstimate(status) {
  const field = el("llm-base-url");
  const hint = el("llm-endpoint-estimate");
  if (!field || !hint) return;
  const show = () => {
    hint.textContent = isLocalEndpoint(field.value.trim())
      ? endpointEstimate(status.hardware, status.budget)
      : "";
  };
  field.addEventListener("input", show);
  show();
}

function updateLlmDownloadProgress(info) {
  const node = document.querySelector(`[data-llm-progress="${info.name}"]`);
  if (!node) return;
  node.textContent = info.state === "running"
    ? `${info.percent}% ${info.detail ?? ""}`.trim()
    : "";
}

// ── embedding models & effective paths ────────────────────────────────

async function refreshEmbeddingModels(selected) {
  const select = el("search-embedding-model");
  if (!select) return;
  // An empty picker used to be the only sign that the catalog call failed —
  // and it is the field that would have fixed the cause, so it says why.
  const note = (text) => {
    const node = el("search-embedding-error");
    if (!node) return;
    node.textContent = text ?? "";
    node.hidden = !text;
  };
  try {
    const catalog = await api.searchModels();
    fillEmbeddingSelect(select, catalog, selected, { hint: el("search-embedding-fit") });
    const hardware = el("search-hardware");
    if (hardware) hardware.textContent = hardwareLine(catalog.hardware, { cpuOnly: true });
    el("search-embedding-cache").textContent = t("settings.embeddingCacheHint", {
      path: catalog.cache_dir,
    });
    note(catalog.cache_dir_error);
  } catch {
    select.disabled = true;
    note(t("settings.embeddingCatalogFailed"));
  }
}

// The data directory is answered by /paths, not by the settings document —
// the field, the "currently" line and the staged-move notice all come from
// there, so they stay in step after a move has been scheduled or cancelled.
function applyDataDir(paths) {
  const field = el("general-data-dir");
  if (!field) return;
  field.placeholder = paths.data_default ?? "";
  el("data-dir-hint").textContent = t("settings.dataDirCurrent", { path: paths.data_dir });
  const pending = el("data-dir-pending");
  pending.hidden = !paths.data_pending;
  pending.textContent = paths.data_pending
    ? t("settings.dataDirPending", { path: paths.data_pending })
    : "";
}

async function refreshPaths() {
  const host = el("workspaces-hint");
  if (!host) return;
  try {
    const paths = await api.getPaths();
    host.textContent = t("settings.workspacesCurrent", {
      path: paths.workspaces_dir,
      count: paths.project_count,
    });
    const llmDir = el("llm-models-dir-current");
    if (llmDir) {
      llmDir.textContent = t("settings.pathInUse", { path: paths.llm_models_dir });
    }
    applyDataDir(paths);
  } catch {
    host.textContent = "";
  }
}

// ── search index status ───────────────────────────────────────────────

async function refreshSearchStatus() {
  const host = el("search-status");
  if (!host) return;
  let status;
  try {
    status = await api.searchStatus();
  } catch {
    return;
  }
  if (!status.available) {
    host.textContent = t("search.unavailableText");
    const reindex = el("search-reindex");
    if (reindex) reindex.disabled = true;
    return;
  }
  const parts = [
    t("settings.searchStatus", { files: status.files_indexed, chunks: status.chunk_count }),
  ];
  if (status.configured_label) parts.push(status.configured_label);
  if (status.last_index) parts.push(t("settings.searchLastIndex", { date: status.last_index }));
  if (status.model_mismatch) parts.push(t("settings.searchModelMismatch"));
  if (status.stale_index) parts.push(t("settings.searchStaleIndex"));
  host.textContent = parts.join(" — ");
  host.classList.toggle("warn", Boolean(status.model_mismatch || status.stale_index));
}

// ── public API keys ───────────────────────────────────────────────────

async function refreshApiKeys() {
  const host = el("apikey-list");
  if (!host) return;
  let keys;
  try {
    keys = await api.listApiKeys();
  } catch {
    return;
  }

  if (!keys.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = t("settings.apiNoKeys");
    host.replaceChildren(p);
    return;
  }

  host.replaceChildren(...keys.map((key) => {
    const row = document.createElement("div");
    row.className = "model-row";
    const name = document.createElement("span");
    name.className = "model-name";
    name.textContent = `${key.name} (${key.prefix}…)`;
    const meta = document.createElement("span");
    meta.className = "muted small";
    meta.textContent = key.last_used_at
      ? t("settings.apiKeyLastUsed", { date: key.last_used_at })
      : t("settings.apiKeyNeverUsed");
    row.append(name, meta, Object.assign(document.createElement("span"), { className: "spacer" }));
    row.append(iconButton("delete", t("common.delete"), async () => {
      const ok = await confirmDelete({
        message: t("settings.apiKeyDeleteConfirm", { name: key.name }),
      });
      if (!ok) return;
      try {
        await api.deleteApiKey(key.id);
        toast(t("settings.apiKeyDeleted"));
        await refreshApiKeys();
      } catch (error) {
        toast(error.message);
      }
    }));
    return row;
  }));
}

function showNewApiKey(key) {
  const host = el("apikey-new");
  if (!host) return;
  host.hidden = false;
  const label = document.createElement("p");
  label.className = "small";
  label.textContent = t("settings.apiKeyCreated");
  const value = document.createElement("code");
  value.className = "apikey-value";
  value.textContent = key;
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "text-btn small-btn";
  copy.textContent = t("settings.apiKeyCopy");
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(key);
      toast(t("settings.apiKeyCopied"));
    } catch {
      /* clipboard may be unavailable without HTTPS — the key stays visible */
    }
  };
  host.replaceChildren(label, value, copy);
}

// ── app updates ───────────────────────────────────────────────────────

// The version row of the system card: what is running, what the newest
// release is, and one icon button that downloads and installs it. What the
// installation does arrives as `update.progress` events and is shown as a log
// while it runs. It ends with the restart — the new version comes up with an
// empty log and the fresh version number in the row (services/updates.py).

async function refreshUpdate(refresh = false) {
  if (!el("update-row")) return;
  // A check the user asked for must not fail silently — the "?" alone would
  // have to be opened to notice. The one that runs on every page load stays
  // in the "?": a toast on arriving at the settings says nothing anybody
  // asked about.
  const failed = (text) => {
    setHelp(el("update-help"), text);
    if (refresh) toast(text);
  };
  let info;
  try {
    info = await api.updateInfo(refresh);
  } catch (error) {
    failed(t("update.checkFailed", { detail: error.message }));
    return;
  }
  if (!el("update-row")) return; // the request outlived the view

  el("update-current").textContent = `Verba ${info.current}`;
  const button = el("update-install");
  button.disabled = !info.can_install || Boolean(info.install?.running);
  // an icon-only button says what it does through its tooltip, and the filled
  // variant is what makes an installable release stand out from the row
  const action = info.available
    ? t("update.install", { version: info.latest })
    : t("update.upToDate");
  button.title = action;
  button.setAttribute("aria-label", action);
  button.classList.toggle("filled", !button.disabled);

  if (info.error) {
    failed(t("update.checkFailed", { detail: info.error }));
  } else {
    let status = "";
    // the reason a kind of installation cannot update itself comes from the
    // backend, which words it for the user
    if (!info.supported) status = info.reason;
    else if (info.available) status = t("update.availableHint", { version: info.latest });
    else if (info.checked) status = t("update.upToDateHint");
    setHelp(el("update-help"), status);
  }

  const notes = el("update-notes");
  notes.hidden = !(info.available && info.notes);
  notes.textContent = info.notes ?? "";

  showUpdateProgress(info.install ?? {});
}

function showUpdateProgress(install) {
  const lines = install.log ?? [];
  const log = el("update-log");
  const bar = el("update-bar");
  el("update-log-title").hidden = lines.length === 0;
  log.hidden = lines.length === 0;
  log.textContent = lines.join("\n");
  log.scrollTop = log.scrollHeight;
  bar.hidden = !install.running;
  bar.firstElementChild.style.width = `${install.percent ?? 0}%`;
}

// ── server packages ───────────────────────────────────────────────────

// Only a Linux server updates its own operating system, so the whole row
// stays out of the card everywhere else — the backend decides that
// (services/osupdate.py). What apt says arrives as `system.upgrade` events
// and is shown as a growing log, which is the point of the button: an
// administrator watches what happens on the machine.

async function refreshOsUpdate() {
  const section = el("os-section");
  if (!section) return;
  let info;
  try {
    info = await api.osUpdateInfo();
  } catch {
    return; // the section stays as it is — nothing here is worth an error
  }
  if (!el("os-section")) return; // the request outlived the view
  section.hidden = !info.supported;
  if (!info.supported) return;

  const button = el("os-run");
  button.disabled = !info.can_run;
  button.classList.toggle("filled", info.can_run);
  // while apt runs, the choice for that run has been made
  el("os-full").disabled = !info.can_run;
  nameOsAction();

  const run = info.run ?? {};
  let status;
  if (run.running) status = t("osUpdate.running");
  else if (run.error) status = t("osUpdate.failed", { detail: run.error });
  else if (run.reboot) status = t("osUpdate.reboot");
  else if (run.finished_at) status = t("osUpdate.done");
  // why the button is off comes from the backend, which words it for the user
  else status = info.reason || t("osUpdate.hint");
  setHelp(el("os-help"), status);

  showOsProgress(run);
}

function nameOsAction() {
  const button = el("os-run");
  if (!button) return;
  const action = el("os-full")?.checked ? t("osUpdate.runFull") : t("osUpdate.run");
  button.title = action;
  button.setAttribute("aria-label", action);
}

function showOsProgress(run) {
  const lines = run.log ?? [];
  const log = el("os-log");
  el("os-log-title").hidden = lines.length === 0;
  log.hidden = lines.length === 0;
  log.textContent = lines.join("\n");
  log.scrollTop = log.scrollHeight;
}

// ── live resource monitor ─────────────────────────────────────────────

// How often the bars ask for new numbers. The backend keeps a reading of
// about this age, so a faster poll would only repeat itself.
const MONITOR_INTERVAL_MS = 2000;

let monitorTimer = null;
let monitorWake = null;

/**
 * The bars this reading has, in order.
 *
 * Every bar carries its own maximum, because the four are not the same kind
 * of number: a load is a share of 100 %, a memory is full when the machine's
 * own memory is gone. Without a graphics card the two GPU bars are left out
 * entirely — an empty bar would claim the card is idle.
 */
function resourceBars(data) {
  const gb = (mb) => (mb / 1024).toFixed(1);
  const percent = (value) =>
    value == null ? t("monitor.unavailable") : t("monitor.percent", { value: value.toFixed(0) });
  const memory = (used, total) =>
    total ? t("monitor.ofTotal", { used: gb(used), total: gb(total) }) : t("monitor.unavailable");

  const bars = [
    {
      key: "cpu",
      label: t("monitor.cpu"),
      value: data.cpu_percent ?? 0,
      max: 100,
      text: percent(data.cpu_percent),
    },
    {
      key: "ram",
      label: t("monitor.ram"),
      value: data.ram_used_mb,
      max: data.ram_total_mb,
      text: memory(data.ram_used_mb, data.ram_total_mb),
    },
  ];
  if (data.vram_total_mb) {
    bars.push(
      {
        key: "gpu",
        label: data.gpu_name || t("monitor.gpu"),
        value: data.gpu_percent ?? 0,
        max: 100,
        text: percent(data.gpu_percent),
      },
      {
        key: "vram",
        label: t("monitor.vram"),
        value: data.vram_used_mb,
        max: data.vram_total_mb,
        text: memory(data.vram_used_mb, data.vram_total_mb),
      },
    );
  }
  return bars;
}

function buildResourceBar(bar) {
  const node = html`
    <div class="resource resource-${bar.key}" data-key="${bar.key}">
      <div class="resource-head">
        <span class="resource-label">${bar.label}</span>
        <span class="resource-value"></span>
      </div>
      <div class="progressbar" role="progressbar" aria-valuemin="0"><div></div></div>
    </div>
  `;
  return node.firstElementChild;
}

/**
 * Show one reading. The rows are only rebuilt when the set of bars itself
 * changes (a graphics card that appears) — otherwise the widths are moved on
 * the existing elements, which is what lets CSS animate them instead of
 * making them jump.
 */
function renderResources(data) {
  const host = el("resource-bars");
  if (!host) return;
  const bars = resourceBars(data);
  const keys = bars.map((bar) => bar.key).join(",");
  if (host.dataset.keys !== keys) {
    host.dataset.keys = keys;
    host.replaceChildren(...bars.map(buildResourceBar));
  }
  for (const bar of bars) {
    const row = host.querySelector(`[data-key="${bar.key}"]`);
    if (!row) continue;
    row.querySelector(".resource-label").textContent = bar.label;
    row.querySelector(".resource-value").textContent = bar.text;
    const meter = row.querySelector(".progressbar");
    const share = bar.max > 0 ? Math.max(0, Math.min(1, bar.value / bar.max)) : 0;
    meter.firstElementChild.style.width = `${(share * 100).toFixed(1)}%`;
    meter.setAttribute("aria-valuemax", String(bar.max));
    meter.setAttribute("aria-valuenow", String(Math.round(bar.value)));
    meter.setAttribute("aria-valuetext", `${bar.label}: ${bar.text}`);
    meter.setAttribute("aria-label", bar.label);
  }
}

/**
 * Poll for as long as the settings page is on screen — and no longer.
 *
 * A tab in the background is not on screen: it keeps its bars but stops
 * asking, and reads once more the moment it is looked at again. The first
 * reading is taken regardless, so a page built in a background tab is never
 * shown with empty bars.
 */
function startResourceMonitor() {
  stopResourceMonitor();
  const tick = async (always = false) => {
    if (!el("resource-bars")) return stopResourceMonitor(); // the view moved on
    if (document.hidden && !always) return; // a tab nobody looks at needs no numbers
    try {
      renderResources(await api.systemResources());
    } catch {
      // a reading that failed is not worth a message: the bars keep what
      // they show and the next tick tries again
    }
  };
  monitorWake = () => {
    if (!document.hidden) tick(true);
  };
  document.addEventListener("visibilitychange", monitorWake);
  tick(true);
  monitorTimer = setInterval(tick, MONITOR_INTERVAL_MS);
}

function stopResourceMonitor() {
  if (monitorTimer) clearInterval(monitorTimer);
  if (monitorWake) document.removeEventListener("visibilitychange", monitorWake);
  monitorTimer = null;
  monitorWake = null;
}

// ── system info ───────────────────────────────────────────────────────

async function refreshSystemInfo() {
  const host = el("system-info");
  if (!host) return;
  let info;
  try {
    info = await api.systemInfo();
  } catch {
    return;
  }

  const gb = (mb) => (mb / 1024).toFixed(1);
  const rows = [
    // the version has its own row above, together with the update button
    [t("system.os"), `${info.os} (${info.os_version})`],
    [t("system.python"), info.python],
    [t("system.cpu"), `${info.cpu_model} — ${t("system.cores", { count: info.cpu_cores })}`],
    [
      t("system.ram"),
      info.ram_total_mb
        ? t("system.freeOfTotal", { free: gb(info.ram_available_mb), total: gb(info.ram_total_mb) })
        : "—",
    ],
    [
      t("system.gpu"),
      info.gpu.name
        ? `${info.gpu.name} — ${t("system.vram")}: ${t("system.freeOfTotal", {
            free: gb(info.gpu.vram_free_mb), total: gb(info.gpu.vram_total_mb),
          })}`
        : t("llmModels.noGpu"),
    ],
    ["ffmpeg", info.ffmpeg ? t("models.installed") : t("system.missing")],
  ];

  renderCudaSection(info.cuda);

  host.replaceChildren(...rows.map(([label, value]) => {
    const row = document.createElement("div");
    row.className = "info-row";
    row.append(
      Object.assign(document.createElement("dt"), { textContent: label }),
      Object.assign(document.createElement("dd"), { textContent: value }),
    );
    return row;
  }));
}

/**
 * The GPU acceleration row: only shown where it means anything — an NVIDIA
 * GPU that the transcription is allowed to use. Three states, because they
 * lead to three different actions: ready (nothing to do), libraries missing
 * (one click installs them) and a GPU whose driver does not answer, where no
 * download helps and the container's device nodes are the place to look.
 */
function renderCudaSection(state) {
  const section = el("cuda-section");
  if (!section) return;
  section.hidden = !state?.applies;
  if (!state?.applies) return;
  const button = el("cuda-install");
  button.hidden = !state.installable;
  button.disabled = false;
  let status;
  if (state.ok) status = t("cuda.ready");
  else if (!state.driver) status = t("cuda.noDriver");
  else status = t("cuda.missing");
  setHelp(el("cuda-help"), status);
}

// Called by the router when another view takes over.
export function destroy() {
  stopResourceMonitor();
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  unsubscribe?.();
  unsubscribe = null;
}
