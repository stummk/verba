// All languages Whisper supports (ISO codes). Used for transcription language
// selection, translation targets and search filters. Display names are
// localized via the browser's Intl.DisplayNames — the UI catalog itself stays
// de/en/ru, but language names follow the UI language automatically.
// tests/test_pipeline.py guards that this list matches the backend's
// services/languages.py.

import { currentLanguage } from "./i18n.js";

const WHISPER_LANGUAGES = [
  "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
  "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
  "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
  "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
  "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
  "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
  "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
  "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
  "yi", "yo", "yue", "zh",
];

// shown first in selects — the most common targets in this app's context
const PRIORITY = ["de", "en", "ru"];

// localization state is per UI locale (a UI language change reloads the page,
// so in practice one cached instance covers the whole session)
let cachedLocale = null;
let displayNames = null;
let sortedCache = null;

function ensureLocale() {
  const locale = currentLanguage();
  if (locale === cachedLocale) return;
  cachedLocale = locale;
  sortedCache = null;
  try {
    displayNames = new Intl.DisplayNames([locale], { type: "language" });
  } catch {
    displayNames = null;
  }
}

export function languageName(code) {
  ensureLocale();
  try {
    return displayNames?.of(code) ?? code;
  } catch {
    return code;
  }
}

/** The one display convention for language options: "Deutsch (de)". */
export function languageLabel(code) {
  return `${languageName(code)} (${code})`;
}

/** [{code, name}] — priority languages first, the rest sorted by local name. */
export function sortedLanguages() {
  ensureLocale();
  if (sortedCache) return sortedCache;
  const collator = new Intl.Collator(cachedLocale);
  const rest = WHISPER_LANGUAGES.filter((code) => !PRIORITY.includes(code))
    .map((code) => ({ code, name: languageName(code) }))
    .sort((a, b) => collator.compare(a.name, b.name));
  sortedCache = [...PRIORITY.map((code) => ({ code, name: languageName(code) })), ...rest];
  return sortedCache;
}

/** Fill a <select> with all languages; value stays the ISO code.
 *  label(name, code) customizes the option text (default "Name (code)"). */
export function fillLanguageSelect(select, { placeholder = null, selected = "", label = null } = {}) {
  if (placeholder !== null) select.append(new Option(placeholder, ""));
  for (const { code, name } of sortedLanguages()) {
    select.append(new Option(label ? label(name, code) : languageLabel(code), code));
  }
  select.value = selected;
}

// ── flags ───────────────────────────────────────────────────────────────
//
// A flag is a country's, not a language's, so only languages that are the
// official or dominant language of one state get one. Regional and
// stateless languages (Catalan, Basque, Tibetan, Cantonese, Latin, …)
// deliberately get none — picking a state for them would be a claim, not a
// label. They fall back to the globe, and the tooltip names them anyway.
const REGIONS = {
  af: "ZA", am: "ET", ar: "SA", az: "AZ", be: "BY", bg: "BG", bn: "BD",
  bs: "BA", cs: "CZ", da: "DK", de: "DE", el: "GR", en: "GB", es: "ES",
  et: "EE", fa: "IR", fi: "FI", fo: "FO", fr: "FR", he: "IL", hi: "IN",
  hr: "HR", ht: "HT", hu: "HU", hy: "AM", id: "ID", is: "IS", it: "IT",
  ja: "JP", ka: "GE", kk: "KZ", km: "KH", ko: "KR", lb: "LU", lo: "LA",
  lt: "LT", lv: "LV", mg: "MG", mk: "MK", mn: "MN", ms: "MY", mt: "MT",
  my: "MM", ne: "NP", nl: "NL", nn: "NO", no: "NO", pl: "PL", ps: "AF",
  pt: "PT", ro: "RO", ru: "RU", si: "LK", sk: "SK", sl: "SI", so: "SO",
  sq: "AL", sr: "RS", sv: "SE", sw: "TZ", tg: "TJ", th: "TH", tk: "TM",
  tl: "PH", tr: "TR", uk: "UA", ur: "PK", uz: "UZ", vi: "VN", zh: "CN",
};

const FIRST_INDICATOR = 0x1f1e6; // 🇦 — the regional indicator letters
const LETTER_A = 65;

/** The two-letter region of a language, or "" when it has no single one. */
export function languageRegion(code) {
  return REGIONS[code] ?? "";
}

/** The flag emoji of a language ("" when it has no region). */
export function languageFlag(code) {
  const region = languageRegion(code);
  if (!region) return "";
  return String.fromCodePoint(
    ...[...region].map((letter) => FIRST_INDICATOR + letter.charCodeAt(0) - LETTER_A)
  );
}

// Windows renders a flag emoji as its two indicator letters instead of a
// flag — Segoe UI Emoji carries no flags. The pair then takes twice the
// width of a single letter, which is what this measures. Where flags do not
// arrive, the chips show the globe instead of two stray letters.
let flagSupport = null;

export function flagsRenderable() {
  if (flagSupport !== null) return flagSupport;
  flagSupport = false;
  try {
    const context = document.createElement("canvas").getContext("2d");
    if (context) {
      const pair = context.measureText(languageFlag("de")).width;
      const single = context.measureText(String.fromCodePoint(FIRST_INDICATOR)).width;
      flagSupport = single > 0 && pair < single * 1.5;
    }
  } catch {
    flagSupport = false;
  }
  return flagSupport;
}
