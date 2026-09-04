// Settings: UI language, whisper defaults, LLM, storage/logs, model management.

import { api } from "../api.js";
import { el, html, raw, toast } from "../dom.js";
import { fillEmbeddingSelect } from "../embeddings.js";
import { applyFitHint, endpointEstimate, fitBadge, hardwareLine, isLocalEndpoint } from "../hardware.js";
import { iconButton, iconSvg } from "../icons.js";
import { SUPPORTED_LANGUAGES, currentLanguage, t } from "../i18n.js";
import { jobCardHost } from "../jobs.js";
import { fillLanguageSelect } from "../languages.js";
import { mountLlamaInstaller } from "../llamainstall.js";
import { on } from "../ws.js";

let unsubscribe = null;
let unsubscribers = [];
let llmInstallerCleanup = null;

export async function render(view) {
  let settings = await api.getSettings();
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
          <div>
            <label for="ui-language">${t("settings.uiLanguage")}</label>
            <select id="ui-language"></select>
          </div>
          <div>
            <label>${t("settings.docs")}</label>
            <a class="btn icon-btn" href="#/docs"
               title="${t("settings.docsOpen")}" aria-label="${t("settings.docsOpen")}"
               >${raw(iconSvg("help"))}</a>
            <p class="hint">${t("settings.docsHint")}</p>
          </div>
        </div>
      </div>

      <div class="card" id="card-transcribe">
        <h2>${t("settings.whisper")}</h2>
        <div class="form-grid">
          <div>
            <label for="whisper-model">${t("settings.model")}</label>
            <input id="whisper-model" value="${settings.whisper.model}" list="model-list">
            <datalist id="model-list"></datalist>
            <p class="hint">${t("settings.modelHint")}</p>
          </div>
          <div>
            <label for="whisper-models-dir">${t("settings.modelsDir")}</label>
            <input id="whisper-models-dir" value="${settings.whisper.models_dir}"
                   placeholder="${t("settings.modelsDirPlaceholder")}">
          </div>
          <div>
            <label for="whisper-device">${t("settings.device")}</label>
            <select id="whisper-device">
              <option value="auto">${t("settings.deviceAuto")}</option>
              <option value="cuda">${t("settings.deviceCuda")}</option>
              <option value="cpu">${t("settings.deviceCpu")}</option>
            </select>
          </div>
          <div>
            <label for="whisper-compute">${t("settings.compute")}</label>
            <select id="whisper-compute">
              <option value="auto">${t("settings.computeAuto")}</option>
              <option>int8</option><option>int8_float16</option>
              <option>float16</option><option>float32</option>
            </select>
          </div>
          <div>
            <label for="whisper-language">${t("settings.language")}</label>
            <select id="whisper-language"></select>
          </div>
        </div>
        <h3 class="subhead">${t("models.title")}</h3>
        <p class="muted small">${t("models.intro")}</p>
        <p class="small" id="whisper-hardware"></p>
        <div id="model-list-host"></div>
        <p class="hint">${t("models.fitHint")}</p>
      </div>

      <div class="card" id="card-ai">
        <h2>${t("settings.navAi")}</h2>
        <p class="muted small">${t("settings.aiIntro")}</p>
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

        <div id="llm-reasoning-field" hidden>
          <label for="llm-reasoning">${t("settings.reasoning")}</label>
          <select id="llm-reasoning">
            <option value="off">${t("settings.reasoningOff")}</option>
            <option value="low">${t("settings.reasoningLow")}</option>
            <option value="auto">${t("settings.reasoningAuto")}</option>
          </select>
          <p class="hint">${t("settings.reasoningHint")}</p>
        </div>

        <div id="llm-local-section" hidden>
          <p class="small" id="llm-hardware"></p>
          <div id="llm-binary"></div>
          <div id="llm-catalog"></div>
          <div class="form-grid">
            <div>
              <label for="llm-local-model">${t("settings.llmLocalModel")}</label>
              <select id="llm-local-model"></select>
              <p class="hint">${t("settings.llmLocalModelHint")}</p>
            </div>
            <div>
              <label for="llm-models-dir">${t("settings.llmModelsDir")}</label>
              <input id="llm-models-dir" value="${settings.llm.models_dir ?? ""}">
              <p class="hint">${t("settings.llmModelsDirHint")}</p>
              <p class="hint" id="llm-models-dir-current"></p>
            </div>
          </div>
        </div>

        <div id="llm-openai-section" hidden>
          <div class="form-grid">
            <div>
              <label for="llm-base-url">${t("settings.baseUrl")}</label>
              <input id="llm-base-url" value="${settings.llm.base_url}"
                     placeholder="https://api.openai.com/v1">
              <p class="small" id="llm-endpoint-estimate"></p>
            </div>
            <div>
              <label for="llm-api-key">${t("settings.apiKey")}</label>
              <input id="llm-api-key" type="password" value="${settings.llm.api_key}" autocomplete="off">
              <p class="hint">${t("settings.apiKeyHint")}</p>
            </div>
            <div>
              <label for="llm-model">${t("settings.llmModel")}</label>
              <input id="llm-model" value="${settings.llm.mode === "openai" ? settings.llm.model : ""}"
                     placeholder="gpt-4o-mini">
            </div>
            <div>
              <label>&nbsp;</label>
              <button type="button" class="text-btn" id="llm-test">${t("settings.llmTest")}</button>
              <p class="hint" id="llm-test-result"></p>
            </div>
          </div>
        </div>
      </div>

      <div class="card" id="card-search">
        <h2>${t("settings.searchTitle")}</h2>
        <p class="muted small">${t("settings.searchIntro")}</p>
        <p class="small" id="search-status"></p>
        <p class="small" id="search-hardware"></p>
        <div id="search-jobs" hidden></div>
        <div class="form-grid">
          <div>
            <label for="search-embedding-model">${t("settings.embeddingModel")}</label>
            <select id="search-embedding-model"></select>
            <p class="hint">${t("settings.embeddingModelHint")}</p>
            <p class="small" id="search-embedding-fit"></p>
          </div>
          <div>
            <label for="search-embeddings-dir">${t("settings.embeddingsDir")}</label>
            <input id="search-embeddings-dir"
                   value="${settings.search?.embeddings_dir ?? ""}">
            <p class="hint">${t("settings.embeddingsDirHint")}</p>
            <p class="hint" id="search-embedding-cache"></p>
          </div>
          <div>
            <label>&nbsp;</label>
            <button type="button" class="text-btn" id="search-reindex">${t("settings.reindex")}</button>
            <p class="hint">${t("settings.reindexHint")}</p>
          </div>
        </div>
      </div>

      <div class="card" id="card-api">
        <h2>${t("settings.apiTitle")}</h2>
        <p class="muted small">${t("settings.apiIntro")}</p>
        <p class="small"><code>${location.origin}/v1/audio/transcriptions</code></p>
        <div id="apikey-new" class="apikey-new" hidden></div>
        <div id="apikey-list"></div>
        <div class="form-grid">
          <div>
            <label for="apikey-name">
              ${t("settings.apiKeyName")}<span class="required-mark" aria-hidden="true">*</span>
            </label>
            <input id="apikey-name" maxlength="100" autocomplete="off"
                   required aria-required="true">
          </div>
          <div>
            <label>&nbsp;</label>
            <button type="button" class="icon-btn" id="apikey-create" disabled
                    title="${t("settings.apiKeyCreate")}"
                    aria-label="${t("settings.apiKeyCreate")}"
                    >${raw(iconSvg("add"))}</button>
          </div>
        </div>
        <p class="hint">${t("settings.apiDocsHint")}</p>
      </div>

      <div class="card" id="card-storage">
        <h2>${t("settings.storage")}</h2>
        <div id="storage-jobs" hidden></div>
        <div class="form-grid">
          <div>
            <label for="general-data-dir">${t("settings.dataDir")}</label>
            <input id="general-data-dir" value="${settings.general.data_dir}">
            <p class="hint" id="data-dir-hint"></p>
            <p class="hint">${t("settings.dataDirHint")}</p>
            <p class="hint">${t("settings.dataDirRestartHint")}</p>
            <p class="warning-box" id="data-dir-pending" hidden></p>
          </div>
          <div>
            <label for="general-workspaces">${t("settings.workspacesDir")}</label>
            <input id="general-workspaces" value="${settings.general.workspaces_dir}"
                   placeholder="${t("settings.workspacesDirPlaceholder")}">
            <p class="hint" id="workspaces-hint"></p>
            <p class="hint">${t("settings.workspacesMoveHint")}</p>
          </div>
          <div>
            <label for="server-port">${t("settings.port")}</label>
            <input id="server-port" type="number" min="1" max="65535" value="${settings.server.port}">
            <p class="hint">${t("settings.portHint")}</p>
          </div>
          <div>
            <label for="log-level">${t("settings.logLevel")}</label>
            <select id="log-level">
              <option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option>
            </select>
          </div>
          <div>
            <label for="log-retention">${t("settings.logRetention")}</label>
            <input id="log-retention" type="number" min="1" max="365"
                   value="${settings.logging.retention_days}">
            <p class="hint">${t("settings.logRetentionHint")}</p>
          </div>
        </div>
      </div>

      <div class="card" id="card-system">
        <h2>${t("settings.system")}</h2>
        <p class="muted small">${t("system.intro")}</p>
        <div class="model-row" id="update-row">
          <span class="model-name" id="update-current">Verba</span>
          <span class="spacer"></span>
          <button type="button" class="icon-btn" id="update-check"
                  title="${t("update.check")}" aria-label="${t("update.check")}"
                  >${raw(iconSvg("refresh"))}</button>
          <button type="button" class="icon-btn" id="update-install" disabled
                  title="${t("update.upToDate")}" aria-label="${t("update.upToDate")}"
                  >${raw(iconSvg("download"))}</button>
        </div>
        <p class="hint" id="update-status"></p>
        <div class="setup-log" id="update-notes" hidden></div>
        <div class="progressbar" id="update-bar" hidden><div></div></div>
        <p class="small muted" id="update-log-title" hidden>${t("update.logTitle")}</p>
        <div class="setup-log" id="update-log" hidden></div>
        <label class="checkline">
          <input type="checkbox" id="update-auto"> ${t("settings.updateCheck")}
        </label>
        <p class="hint">${t("settings.updateCheckHint")}</p>
        <div id="os-section" hidden>
          <div class="model-row" id="os-row">
            <span class="model-name">${t("osUpdate.title")}</span>
            <span class="spacer"></span>
            <button type="button" class="icon-btn" id="os-run" disabled
                    title="${t("osUpdate.run")}" aria-label="${t("osUpdate.run")}"
                    >${raw(iconSvg("upgrade"))}</button>
          </div>
          <p class="hint" id="os-status"></p>
          <label class="checkline">
            <input type="checkbox" id="os-full"> ${t("osUpdate.full")}
          </label>
          <p class="hint">${t("osUpdate.fullHint")}</p>
          <p class="small muted" id="os-log-title" hidden>${t("osUpdate.logTitle")}</p>
          <div class="setup-log" id="os-log" hidden></div>
        </div>
        <dl class="info-list" id="system-info"></dl>
      </div>

      <div class="card" id="card-account">
        <h2>${t("settings.accountTitle")}</h2>
        <p class="muted small">${t("settings.accountIntro")}</p>
        <div id="account-host"></div>
        <h3 class="subhead">${t("users.title")}</h3>
        <p class="muted small">${t("settings.usersIntro")}</p>
        <a class="btn tonal" href="#/users">${t("settings.usersOpen")}</a>
      </div>

      <div class="actions">
        <button type="submit">${t("common.save")}</button>
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

  // a key without a label cannot be told apart later — the button stays
  // disabled until the field holds something
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
  el("update-check").onclick = async () => {
    el("update-status").textContent = t("update.checking");
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
    el("os-status").textContent = t("osUpdate.running");
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

  // independent sections, each with its own error handling — load in parallel
  await Promise.all([
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

  el("settings-form").onsubmit = async (event) => {
    event.preventDefault();
    const previousLanguage = settings.general.ui_language || "de";
    const payload = {
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
      },
      updates: { check_enabled: el("update-auto").checked },
      server: { port: Number(el("server-port").value) },
      logging: {
        level: el("log-level").value,
        retention_days: Number(el("log-retention").value),
      },
    };
    try {
      const saved = await api.updateSettings(payload);
      if (payload.general.ui_language !== previousLanguage) {
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
      await Promise.all([
        refreshPaths(),
        refreshSearchStatus(),
        refreshEmbeddingModels(settings.search?.embedding_model),
        refreshLlmSection(),
      ]);
    } catch (error) {
      toast(t("settings.saveError", { message: error.message }));
    }
  };
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
          <div>
            <label for="ui-language">${t("settings.uiLanguage")}</label>
            <select id="ui-language"></select>
          </div>
          <div>
            <label>${t("settings.docs")}</label>
            <a class="btn icon-btn" href="#/docs"
               title="${t("settings.docsOpen")}" aria-label="${t("settings.docsOpen")}"
               >${raw(iconSvg("help"))}</a>
            <p class="hint">${t("settings.docsHint")}</p>
          </div>
        </div>
        <div class="actions">
          <button type="submit">${t("common.save")}</button>
        </div>
      </div>
      <div class="card">
        <h2>${t("settings.accountTitle")}</h2>
        <p class="muted small">${t("settings.accountIntro")}</p>
        <div id="account-host"></div>
      </div>
    </form>
  `);

  const uiLanguageSelect = el("ui-language");
  for (const code of SUPPORTED_LANGUAGES) {
    uiLanguageSelect.append(new Option(t(`lang.${code}`), code));
  }
  uiLanguageSelect.value = settings.general.ui_language || currentLanguage();

  el("settings-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      // the payload keeps the shape the backend sent; only the one field a
      // normal user owns is changed
      await api.updateSettings({
        ...settings,
        general: { ...settings.general, ui_language: uiLanguageSelect.value },
      });
      toast(t("settings.saved"));
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
      <div>
        <label for="account-current">${t("login.currentPassword")}</label>
        <input id="account-current" type="password" autocomplete="current-password">
      </div>
      <div>
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
    if (!confirm(t("settings.deleteAccountConfirm"))) return;
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
        row.append(iconButton("delete", t("common.delete"), async () => {
          await api.llmDeleteModel(entry.file).catch((e) => toast(e.message));
          toast(t("models.deleted"));
          await refreshLlmSection();
        }));
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
  try {
    const catalog = await api.searchModels();
    fillEmbeddingSelect(select, catalog, selected, { hint: el("search-embedding-fit") });
    const hardware = el("search-hardware");
    if (hardware) hardware.textContent = hardwareLine(catalog.hardware, { cpuOnly: true });
    el("search-embedding-cache").textContent = t("settings.embeddingCacheHint", {
      path: catalog.cache_dir,
    });
  } catch {
    select.disabled = true;
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
  host.textContent = parts.join(" — ");
  host.classList.toggle("warn", Boolean(status.model_mismatch));
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
  let info;
  try {
    info = await api.updateInfo(refresh);
  } catch (error) {
    el("update-status").textContent = t("update.checkFailed", { detail: error.message });
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

  const status = el("update-status");
  if (info.error) status.textContent = t("update.checkFailed", { detail: info.error });
  // the reason a kind of installation cannot update itself comes from the
  // backend, which words it for the user
  else if (!info.supported) status.textContent = info.reason;
  else if (info.available) status.textContent = t("update.availableHint", { version: info.latest });
  else if (info.checked) status.textContent = t("update.upToDateHint");
  else status.textContent = "";

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
  const status = el("os-status");
  if (run.running) status.textContent = t("osUpdate.running");
  else if (run.error) status.textContent = t("osUpdate.failed", { detail: run.error });
  else if (run.reboot) status.textContent = t("osUpdate.reboot");
  else if (run.finished_at) status.textContent = t("osUpdate.done");
  // why the button is off comes from the backend, which words it for the user
  else status.textContent = info.reason || t("osUpdate.hint");

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

// Called by the router when another view takes over.
export function destroy() {
  unsubscribers.forEach((off) => off());
  unsubscribers = [];
  unsubscribe?.();
  unsubscribe = null;
}
