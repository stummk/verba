// The local LLM setup as one component: a row that installs llama.cpp (or
// reports it as installed), optionally a row for the recommended GGUF model,
// a progress bar and the live log of what the backend is doing — release,
// download, unpacking, system libraries it installs on the way, the version
// check, and the same for a model download.
//
// The first-run wizard and the settings page mount the same thing, so the
// local LLM can be set up in either place and both show the same protocol.
// The binary installation's log is part of the LLM status, so switching the
// wizard step or reloading the page does not lose what happened; the lines of
// a running download are appended as their events arrive.

import { api } from "./api.js";
import { t } from "./i18n.js";
import { on } from "./ws.js";

/**
 * Render the local-LLM installer into `host` and keep it live.
 *
 * @param {HTMLElement} host container that is emptied and filled
 * @param {object} options
 *   status – an LLM status from api.llmStatus(), when the caller already has one
 *   withRecommendedModel – also offer the recommended GGUF model (the wizard;
 *     the settings page has the whole catalog and needs no second button)
 *   extraControls – (status) => Node[] appended to the llama.cpp row (the
 *     settings page puts its "stop server" button there)
 *   onChanged – called with the fresh status after an installation ended
 * @returns {() => void} unsubscribes from the event stream
 */
export function mountLlamaInstaller(host, options = {}) {
  let status = options.status ?? null;
  let lines = [...(status?.install?.log ?? [])];

  const binaryRow = document.createElement("div");
  binaryRow.className = "model-row";
  const modelRow = document.createElement("div");
  modelRow.className = "model-row";
  modelRow.hidden = !options.withRecommendedModel;
  const bar = document.createElement("div");
  bar.className = "progressbar";
  bar.hidden = true;
  const fill = document.createElement("div");
  bar.append(fill);
  const logTitle = document.createElement("p");
  logTitle.className = "small muted";
  logTitle.textContent = t("llmModels.installLog");
  logTitle.hidden = true;
  const log = document.createElement("div");
  log.className = "setup-log";
  log.hidden = true;
  host.replaceChildren(binaryRow, modelRow, bar, logTitle, log);

  function label(text) {
    const node = document.createElement("span");
    node.className = "model-name";
    node.textContent = text;
    return node;
  }

  function spacer() {
    const node = document.createElement("span");
    node.className = "spacer";
    return node;
  }

  function badge(text) {
    const node = document.createElement("span");
    node.className = "badge badge-done";
    node.textContent = text;
    return node;
  }

  function actionButton(text, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "text-btn small-btn";
    button.textContent = text;
    button.onclick = async () => {
      button.disabled = true;
      appendLine(text);
      try {
        await onClick();
      } catch (error) {
        // a refused start never reaches the event stream — say so in the log
        appendLine(error.message);
        setBar(0, false);
        button.disabled = false;
      }
    };
    return button;
  }

  function renderRows() {
    binaryRow.replaceChildren(label("llama.cpp (llama-server)"), spacer());
    if (status?.binary_installed) {
      binaryRow.append(badge(t("models.installed")));
    } else {
      const install = actionButton(t("llmModels.installBinary"), () => api.llmSetup());
      install.disabled = Boolean(status?.install?.running);
      binaryRow.append(install);
    }
    for (const control of options.extraControls?.(status) ?? []) binaryRow.append(control);

    if (!options.withRecommendedModel) return;
    const recommended = status?.recommended;
    modelRow.hidden = !recommended;
    if (!recommended) return;
    modelRow.replaceChildren(label(recommended.label), spacer());
    const installed = (status?.installed ?? []).some((model) => model.file === recommended.file);
    modelRow.append(installed
      ? badge(t("models.installed"))
      : actionButton(
        t("llmModels.downloadRecommended"),
        () => api.llmDownload(recommended.name),
      ));
  }

  function appendLine(text) {
    if (!text || lines.at(-1) === text) return;
    lines.push(text);
    show();
  }

  function show() {
    if (!lines.length) return;
    logTitle.hidden = false;
    log.hidden = false;
    log.textContent = lines.join("\n");
    log.scrollTop = log.scrollHeight;
  }

  function setBar(percent, running) {
    bar.hidden = !running;
    fill.style.width = `${percent ?? 0}%`;
  }

  renderRows();
  show();
  if (status?.install?.running) setBar(status.install.percent, true);

  const unsubscribe = on("model.download", async (info) => {
    if (info.scope !== "llm-binary" && info.scope !== "llm") return;
    if (info.scope === "llm" && !options.withRecommendedModel) return;
    if (!host.isConnected) return; // the view moved on; the next mount reads the state
    appendLine(info.detail);
    setBar(info.percent, info.state === "running");
    if (info.state === "running") return;
    status = await api.llmStatus().catch(() => status);
    renderRows();
    options.onChanged?.(status);
  });
  return () => unsubscribe();
}
