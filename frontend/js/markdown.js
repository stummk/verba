// Render model output as markdown — safely.
//
// LLM answers come back as markdown (lists, bold, code, tables), and reading
// them as raw text loses exactly the structure that makes an answer usable.
// marked itself does not sanitize, so the parsed fragment is walked and
// everything outside a small allowlist is dropped: an `<img onerror=…>` or a
// `<script>` inside a model answer must never become markup in our own page.

import { marked } from "/vendor/marked.esm.js";

// Everything a helpful answer needs — and nothing that can load or execute.
const ALLOWED = new Set([
  "P", "BR", "HR", "STRONG", "EM", "DEL", "CODE", "PRE", "BLOCKQUOTE",
  "UL", "OL", "LI", "H1", "H2", "H3", "H4", "H5", "H6",
  "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "A", "SPAN",
]);
// Tags whose text content is markup or noise, not content — dropped whole.
const DROPPED = new Set([
  "SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "LINK", "META", "BASE",
  "FORM", "INPUT", "BUTTON", "TEMPLATE", "NOSCRIPT", "SVG", "MATH",
]);
const ALLOWED_ATTRIBUTES = { A: ["href", "title"] };
const SAFE_HREF = /^(https?:|mailto:|#)/i;

export function renderMarkdown(host, text) {
  const template = document.createElement("template");
  template.innerHTML = marked.parse(text ?? "");
  sanitize(template.content);
  host.replaceChildren(template.content);
}

// Children first: an unwrapped element's children have then already been
// checked, so nothing can slip past by hiding inside a disallowed tag.
function sanitize(parent) {
  for (const node of [...parent.children]) sanitizeElement(node);
}

function sanitizeElement(node) {
  if (DROPPED.has(node.tagName)) {
    node.remove();
    return;
  }
  sanitize(node);
  if (!ALLOWED.has(node.tagName)) {
    node.replaceWith(...node.childNodes); // keep the text, drop the tag
    return;
  }
  const allowed = ALLOWED_ATTRIBUTES[node.tagName] ?? [];
  for (const attribute of [...node.attributes]) {
    if (!allowed.includes(attribute.name.toLowerCase())) {
      node.removeAttribute(attribute.name);
    }
  }
  if (node.tagName === "A") {
    const href = (node.getAttribute("href") ?? "").trim();
    if (SAFE_HREF.test(href)) {
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    } else {
      node.removeAttribute("href"); // javascript:, data:, … stay unclickable
    }
  }
}
