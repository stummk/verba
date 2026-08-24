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
