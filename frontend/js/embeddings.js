// The embedding-model picker, shared by the settings page and the setup
// wizard: a curated catalog comes from the backend (GET /api/search/models),
// so the option text can name size, languages and speed instead of leaving
// the user to look up a model id.

import { t } from "./i18n.js";

export function fillEmbeddingSelect(select, catalog, selected) {
  select.replaceChildren(
    ...catalog.models.map((model) => {
      const text = t("settings.embeddingOption", {
        label: model.label,
        size: (model.size_mb / 1024).toFixed(1),
        languages: model.languages,
        speed: t(`settings.embeddingSpeed.${model.speed}`),
      });
      // a model already lying in the directory costs no download at all
      return new Option(model.present ? `${text} · ${t("settings.embeddingPresent")}` : text,
        model.name);
    }),
  );
  select.value = catalog.models.some((model) => model.name === selected)
    ? selected
    : catalog.default;
}
