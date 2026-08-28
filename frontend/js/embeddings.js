// The embedding-model picker, shared by the settings page and the setup
// wizard: a curated catalog comes from the backend (GET /api/search/models),
// so the option text can name size, languages and speed instead of leaving
// the user to look up a model id.

import { applyFitHint } from "./hardware.js";
import { t } from "./i18n.js";

// `hint` (optional) is a node that carries the memory verdict for whatever is
// currently selected — the index is built on this machine, so a model too
// large for its RAM is worth saying before the reindex, not after.
export function fillEmbeddingSelect(select, catalog, selected, { hint } = {}) {
  select.replaceChildren(
    ...catalog.models.map((model) => {
      const text = t("settings.embeddingOption", {
        label: model.label,
        size: (model.size_mb / 1024).toFixed(1),
        languages: model.languages,
        speed: t(`settings.embeddingSpeed.${model.speed}`),
      });
      const parts = [text];
      // a model already lying in the directory costs no download at all
      if (model.present) parts.push(t("settings.embeddingPresent"));
      if (model.fit?.level && model.fit.level !== "ok") {
        parts.push(t(`models.fit${model.fit.level === "no" ? "No" : "Tight"}`));
      }
      return new Option(parts.join(" · "), model.name);
    }),
  );
  select.value = catalog.models.some((model) => model.name === selected)
    ? selected
    : catalog.default;
  if (!hint) return;
  const show = () => {
    const model = catalog.models.find((entry) => entry.name === select.value);
    applyFitHint(hint, model?.fit);
  };
  select.addEventListener("change", show);
  show();
}
