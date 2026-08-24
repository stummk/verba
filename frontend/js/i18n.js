// Tiny i18n runtime: flat key catalogs in /i18n/<lang>.json, "{var}" interpolation.

export const SUPPORTED_LANGUAGES = ["de", "en", "ru"];
const DEFAULT_LANGUAGE = "de";

let lang = DEFAULT_LANGUAGE;
let catalog = {};
let fallback = {};

async function loadCatalog(code) {
  const response = await fetch(`/i18n/${code}.json`);
  if (!response.ok) throw new Error(`i18n catalog ${code} missing`);
  return response.json();
}

export async function initI18n(preferred) {
  lang = SUPPORTED_LANGUAGES.includes(preferred) ? preferred : DEFAULT_LANGUAGE;
  catalog = await loadCatalog(lang);
  fallback = lang === DEFAULT_LANGUAGE ? catalog : await loadCatalog(DEFAULT_LANGUAGE);
  document.documentElement.lang = lang;
  translateStatic();
}

export function t(key, vars = {}) {
  const template = catalog[key] ?? fallback[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (_, name) => `${vars[name] ?? ""}`);
}

export function currentLanguage() {
  return lang;
}

// Translate static markup: <span data-i18n="nav.projects"></span>
export function translateStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
}
