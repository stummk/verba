// The one confirmation dialog in front of every delete.
//
// Nothing that is deleted can be brought back — a file, a PDF or a whole
// selection is gone for good — so every delete asks first. The dialog lives in
// a host at the end of <body>, not in the calling view: a delete usually
// re-renders that view, and a dialog inside it would vanish mid-answer.

import { el, html } from "./dom.js";
import { t } from "./i18n.js";

const HOST_ID = "confirm-host";

function host() {
  let node = el(HOST_ID);
  if (!node) {
    node = document.createElement("div");
    node.id = HOST_ID;
    document.body.appendChild(node);
  }
  return node;
}

/**
 * Ask before deleting. Resolves to true only when the delete button was
 * pressed — cancel, Escape and the backdrop all answer false.
 *
 * @param {{title?: string, message: string, confirmLabel?: string}} options
 * @returns {Promise<boolean>}
 */
export function confirmDelete({ title, message, confirmLabel } = {}) {
  const node = host();
  node.replaceChildren(html`
    <dialog id="confirm-dialog">
      <h2>${title ?? t("confirm.deleteTitle")}</h2>
      <p id="confirm-message">${message}</p>
      <div class="actions">
        <button type="button" class="danger" id="confirm-yes">
          ${confirmLabel ?? t("common.delete")}
        </button>
        <button type="button" class="text-btn" id="confirm-no">${t("common.cancel")}</button>
      </div>
    </dialog>
  `);

  const dialog = el("confirm-dialog");
  return new Promise((resolve) => {
    let settled = false;
    // Every way out ends here, and the answer is given right where the way out
    // is taken: a <dialog> does not reliably report its own close, so waiting
    // for that event alone would leave the caller hanging.
    function finish(answer) {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      node.replaceChildren();
      resolve(answer);
    }
    el("confirm-yes").onclick = () => finish(true);
    el("confirm-no").onclick = () => finish(false);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault(); // Escape: close it here, not next to the promise
      finish(false);
    });
    dialog.addEventListener("close", () => finish(false));
    dialog.onclick = (event) => {
      if (event.target === dialog) finish(false); // the backdrop, not the box
    };
    dialog.showModal();
    el("confirm-no").focus(); // the harmless answer is the one under the finger
  });
}
