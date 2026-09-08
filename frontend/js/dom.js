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

/**
 * Let a row that scrolls sideways be dragged with the mouse.
 *
 * A row of badges wide enough to overflow has no visible scrollbar (hiding it
 * is what keeps a card looking like a card), so grabbing it has to be the way
 * to move it. Touch is left alone: a finger already scrolls the row natively,
 * and taking the pointer events over would only break that.
 *
 * The click that ends a real drag is swallowed — the row usually sits inside
 * a link, and dragging it must not navigate.
 *
 * @param {HTMLElement} node the scrolling element
 */
export function dragScroll(node) {
  // How far the pointer may travel before this counts as a drag rather than a
  // click. A pointer tolerance, not a size — it does not scale with the font.
  const SLOP = 4;
  let origin = null; // {x, scrollLeft} while a button is held
  let dragged = false;
  const overflows = () => node.scrollWidth > node.clientWidth;

  // the grab cursor is a promise: it only appears where there is something to
  // grab, so a row that fits keeps the pointer of the link it sits in
  node.addEventListener("pointerenter", () => {
    node.classList.toggle("scrollable", overflows());
  });
  node.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || !overflows()) return;
    origin = { x: event.clientX, scrollLeft: node.scrollLeft };
    dragged = false;
  });
  node.addEventListener("pointermove", (event) => {
    if (!origin) return;
    const moved = event.clientX - origin.x;
    if (!dragged && Math.abs(moved) < SLOP) return;
    if (!dragged) {
      dragged = true;
      node.classList.add("dragging");
      node.setPointerCapture(event.pointerId);
    }
    event.preventDefault(); // no text selection while dragging
    node.scrollLeft = origin.scrollLeft - moved;
  });
  const release = (event) => {
    if (!origin) return;
    origin = null;
    node.classList.remove("dragging");
    if (node.hasPointerCapture?.(event.pointerId)) node.releasePointerCapture(event.pointerId);
  };
  node.addEventListener("pointerup", release);
  node.addEventListener("pointercancel", release);
  // the click arrives after pointerup, which is where `dragged` still stands
  node.addEventListener("click", (event) => {
    if (!dragged) return;
    event.preventDefault();
    event.stopPropagation();
    dragged = false;
  }, true);
  return node;
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
  // A short confirmation is read at a glance; an error from the LLM endpoint
  // can be several lines long and needs the time to actually be read.
  const readingTime = 3000 + Math.max(0, message.length - 60) * 60;
  toastTimer = setTimeout(() => node.classList.remove("show"), Math.min(readingTime, 15000));
}
