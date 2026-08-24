// Small DOM helpers — no framework, no build step.

export function html(strings, ...values) {
  const template = document.createElement("template");
  template.innerHTML = strings.reduce(
    (out, str, i) => out + str + (i < values.length ? escapeIfString(values[i]) : ""),
    ""
  );
  return template.content;
}

function escapeIfString(value) {
  if (value == null) return "";
  if (value.__raw) return value.html;
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// Mark a string as pre-built, trusted HTML (built from escaped parts).
export function raw(htmlString) {
  return { __raw: true, html: htmlString };
}

// Escape untrusted text for use inside raw() HTML strings.
export function esc(value) {
  return escapeIfString(value);
}

export function el(id) {
  return document.getElementById(id);
}

export function formatDuration(seconds) {
  if (seconds == null) return "–";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

let toastTimer = null;
export function toast(message) {
  let node = document.querySelector(".toast");
  if (!node) {
    node = document.createElement("div");
    node.className = "toast";
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3000);
}
