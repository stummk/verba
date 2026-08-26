// Settings: UI language, whisper defaults, LLM, storage/logs, model management.

import { api } from "../api.js";
import { el, html, toast } from "../dom.js";
import { fillEmbeddingSelect } from "../embeddings.js";
import { iconButton } from "../icons.js";
import { SUPPORTED_LANGUAGES, currentLanguage, t } from "../i18n.js";
import { fillLanguageSelect } from "../languages.js";
import { on } from "../ws.js";

let unsubscribe = null;

export async function render(view) {
  let settings = await api.getSettings();

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
            <a class="btn tonal" href="#/docs">${t("settings.docsOpen")}</a>
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
        <div id="model-list-host"></div>
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
        <div class="form-grid">
          <div>
            <label for="search-embedding-model">${t("settings.embeddingModel")}</label>
            <select id="search-embedding-model"></select>
            <p class="hint">${t("settings.embeddingModelHint")}</p>
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
            <label for="apikey-name">${t("settings.apiKeyName")}</label>
            <input id="apikey-name" maxlength="100" autocomplete="off">
          </div>
          <div>
            <label>&nbsp;</label>
            <button type="button" class="text-btn" id="apikey-create">
              ${t("settings.apiKeyCreate")}
            </button>
          </div>
        </div>
        <p class="hint">${t("settings.apiDocsHint")}</p>
      </div>

      <div class="card" id="card-storage">
        <h2>${t("settings.storage")}</h2>
        <div class="form-grid">
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
        <dl class="info-list" id="system-info"></dl>
      </div>

      <div class="actions">
        <button type="submit">${t("common.save")}</button>
      </div>
    </form>
    </div>
  `);

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

  el("apikey-create").onclick = async () => {
    const name = el("apikey-name").value.trim();
    if (!name) return;
    try {
      const created = await api.createApiKey(name);
      el("apikey-name").value = "";
      showNewApiKey(created.key);
      await refreshApiKeys();
    } catch (error) {
      toast(error.message);
    }
  };

  // independent sections, each with its own error handling — load in parallel
  await Promise.all([
    refreshModels(),
    refreshLlmSection(),
    refreshSystemInfo(),
    refreshEmbeddingModels(settings.search?.embedding_model),
    refreshSearchStatus(),
    refreshPaths(),
    refreshApiKeys(),
  ]);

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
        workspaces_dir: el("general-workspaces").value.trim(),
      },
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
      // keep the settings only: the two job flags are not part of them
      const { workspace_move: _move, reindex_started: _reindex, ...stored } = saved;
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

  const host = el("model-list-host");
  if (host) {
    const installed = new Set([...(models.installed ?? []), ...local]);
    const downloading = new Set(models.downloading ?? []);
    host.replaceChildren(...names.map((name) => modelRow(name, {
      installed: installed.has(name),
      downloading: downloading.has(name),
      custom: !builtin.includes(name),
    })));
  }
}

function modelRow(name, { installed, downloading, custom }) {
  const row = document.createElement("div");
  row.className = "model-row";
  row.append(Object.assign(document.createElement("span"), {
    className: "model-name", textContent: custom ? `📁 ${name}` : name,
  }));
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  row.append(spacer);

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
    const hw = status.hardware;
    const gpu = hw.gpu_name
      ? `${hw.gpu_name} (${Math.round(hw.vram_mb / 1024)} GB VRAM)`
      : t("llmModels.noGpu");
    hardware.textContent = t("llmModels.hardware", {
      ram: Math.round(hw.ram_mb / 1024),
      gpu,
      recommended: status.recommended.label,
    });
  }

  const binaryHost = el("llm-binary");
  if (binaryHost) {
    const row = document.createElement("div");
    row.className = "model-row";
    row.append(Object.assign(document.createElement("span"), {
      className: "model-name", textContent: "llama.cpp (llama-server)",
    }));
    row.append(Object.assign(document.createElement("span"), { className: "spacer" }));
    if (status.binary_installed) {
      row.append(Object.assign(document.createElement("span"), {
        className: "badge badge-done", textContent: t("models.installed"),
      }));
      if (status.server_running) {
        row.append(iconButton("stop", t("llmModels.stopServer"), async () => {
          await api.llmStopServer().catch((e) => toast(e.message));
          await refreshLlmSection();
        }));
      }
    } else {
      const install = document.createElement("button");
      install.type = "button";
      install.className = "text-btn small-btn";
      install.textContent = t("llmModels.installBinary");
      install.onclick = async () => {
        try {
          await api.llmSetup();
          toast(t("llmModels.installStarted"));
        } catch (error) {
          toast(error.message);
        }
      };
      row.append(install);
    }
    const progress = document.createElement("div");
    progress.className = "small muted";
    progress.dataset.llmProgress = "llama.cpp";
    binaryHost.replaceChildren(row, progress);
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
    fillEmbeddingSelect(select, catalog, selected);
    el("search-embedding-cache").textContent = t("settings.embeddingCacheHint", {
      path: catalog.cache_dir,
    });
  } catch {
    select.disabled = true;
  }
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
    [t("system.version"), `Verba ${info.version}`],
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
