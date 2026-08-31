// REST client — relative URLs, damit Reverse-Proxy-Setups funktionieren.

import { t } from "./i18n.js";

// Stable per-browser session id: the queue schedules FIFO per session so one
// user's bulk import cannot starve the others.
function sessionId() {
  let id = localStorage.getItem("verba.session");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2);
    localStorage.setItem("verba.session", id);
  }
  return id;
}

async function request(method, path, body) {
  const options = { method, headers: { "X-Session-Id": sessionId() } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data.detail) detail = data.detail;
    } catch { /* keep default detail */ }
    throw new Error(detail);
  }
  return response.json();
}

// XHR instead of fetch: only XHR reports how many bytes of the request body
// have gone out, and an audio upload is long enough that the UI has to show it.
function upload(path, file, onProgress = null) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded, event.total);
      };
      // body handed over completely — from here the server is working on it
      xhr.upload.onload = () => onProgress(file.size, file.size);
    }
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* keep null */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data?.detail ?? `${xhr.status} ${xhr.statusText}`));
    };
    xhr.onerror = () => reject(new Error(t("app.networkError")));
    xhr.onabort = () => reject(new Error(t("app.networkError")));
    xhr.send(form);
  });
}

export const api = {
  // system & settings
  systemStatus: () => request("GET", "/api/system/status"),
  systemInfo: () => request("GET", "/api/system/info"),
  shutdown: () => request("POST", "/api/system/shutdown"),
  runSetup: (includeOptional = true) =>
    request("POST", "/api/system/setup/run", { include_optional: includeOptional }),
  completeSetup: () => request("POST", "/api/system/setup/complete"),
  getSettings: () => request("GET", "/api/settings"),
  updateSettings: (settings) => request("PUT", "/api/settings", settings),
  getPaths: () => request("GET", "/api/settings/paths"),
  listModels: () => request("GET", "/api/models"),
  getDocs: (lang) => request("GET", `/api/docs?lang=${encodeURIComponent(lang)}`),
  docsAsk: (question, lang) => request("POST", "/api/docs/ask", { question, lang }),

  // projects & files
  listProjects: () => request("GET", "/api/projects"),
  createProject: (name, typeId = null) =>
    request("POST", "/api/projects", { name, type_id: typeId }),
  updateProject: (id, changes) => request("PUT", `/api/projects/${id}`, changes),
  getProject: (id) => request("GET", `/api/projects/${id}`),
  deleteProject: (id, deleteFiles = true) =>
    request("DELETE", `/api/projects/${id}?delete_files=${deleteFiles}`),
  importFiles: (projectId, paths) =>
    request("POST", `/api/projects/${projectId}/files/import`, { paths }),
  uploadFile: (projectId, file, onProgress) =>
    upload(`/api/projects/${projectId}/files/upload`, file, onProgress),
  deleteFile: (fileId) => request("DELETE", `/api/files/${fileId}`),
  updateFileHeader: (fileId, header) =>
    request("PUT", `/api/files/${fileId}/header`, header),
  browse: (path = "") => request("GET", `/api/files/browse?path=${encodeURIComponent(path)}`),

  // models
  downloadModel: (name) => request("POST", "/api/models/download", { name }),
  deleteModel: (name) => request("DELETE", `/api/models?name=${encodeURIComponent(name)}`),
  llmStatus: () => request("GET", "/api/models/llm"),
  llmSetup: () => request("POST", "/api/models/llm/setup"),
  llmDownload: (name) => request("POST", "/api/models/llm/download", { name }),
  llmDeleteModel: (name) => request("DELETE", `/api/models/llm?name=${encodeURIComponent(name)}`),
  llmStopServer: () => request("POST", "/api/models/llm/stop"),
  llmTest: (baseUrl, apiKey) =>
    request("POST", "/api/settings/llm/test", { base_url: baseUrl, api_key: apiKey }),

  // project types
  listTypes: () => request("GET", "/api/types"),
  typeDefaults: () => request("GET", "/api/types/defaults"),
  createType: (name, settings) => request("POST", "/api/types", { name, ...settings }),
  updateType: (id, name, settings) => request("PUT", `/api/types/${id}`, { name, ...settings }),
  deleteType: (id) => request("DELETE", `/api/types/${id}`),
  restoreTypes: () => request("POST", "/api/types/restore-defaults"),

  // LLM pipeline
  processFile: (fileId, options) => request("POST", `/api/files/${fileId}/process`, options),
  processProject: (projectId, options) =>
    request("POST", `/api/projects/${projectId}/process`, options),
  getTexts: (fileId) => request("GET", `/api/files/${fileId}/texts`),

  // PDF export
  exportFile: (fileId, options = {}) =>
    request("POST", `/api/files/${fileId}/export`, { language: "", combine: false, ...options }),
  exportProject: (projectId, options = {}) =>
    request("POST", `/api/projects/${projectId}/export`, {
      language: "", combine: false, ...options,
    }),
  listExports: (projectId) => request("GET", `/api/projects/${projectId}/exports`),
  deleteExport: (projectId, name) =>
    request("DELETE", `/api/projects/${projectId}/exports/${encodeURIComponent(name)}`),
  exportUrl: (projectId, name) =>
    `/api/projects/${projectId}/exports/${encodeURIComponent(name)}`,
  updateText: (fileId, kind, language, content) =>
    request("PUT", `/api/files/${fileId}/texts/${kind}?language=${encodeURIComponent(language)}`,
      { content }),

  // transcription & jobs
  transcribeFile: (fileId, options = {}) =>
    request("POST", `/api/files/${fileId}/transcribe`, options),
  transcribeProject: (projectId, force = false, options = {}) =>
    request("POST", `/api/projects/${projectId}/transcribe?force=${force}`, options),
  getSegments: (fileId) => request("GET", `/api/files/${fileId}/segments`),
  updateSegment: (segmentId, changes) => request("PUT", `/api/segments/${segmentId}`, changes),
  deleteSegment: (segmentId) => request("DELETE", `/api/segments/${segmentId}`),
  transcribeRange: (fileId, startS, endS, options = {}) =>
    request("POST", `/api/files/${fileId}/transcribe-range`,
      { start_s: startS, end_s: endS, ...options }),
  editAudio: (fileId, op, startS, endS) =>
    request("POST", `/api/files/${fileId}/audio/edit`, { op, start_s: startS, end_s: endS }),
  // semantic search
  search: (options) => request("POST", "/api/search", options),
  searchAsk: (options) => request("POST", "/api/search/ask", options),
  searchStatus: () => request("GET", "/api/search/status"),
  searchModels: () => request("GET", "/api/search/models"),
  searchReindex: () => request("POST", "/api/search/reindex"),

  // public API keys
  listApiKeys: () => request("GET", "/api/apikeys"),
  createApiKey: (name) => request("POST", "/api/apikeys", { name }),
  deleteApiKey: (id) => request("DELETE", `/api/apikeys/${id}`),

  listJobs: (activeOnly = false) => request("GET", `/api/jobs?active=${activeOnly}`),
  queueOverview: () => request("GET", "/api/jobs/queue"),
  cancelJob: (jobId) => request("POST", `/api/jobs/${jobId}/cancel`),
};
