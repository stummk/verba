// Service worker: cache the complete app shell, never cache API or WebSocket.
// The shell list is checked by tests/test_pwa.py — new frontend files must be
// added here so the app keeps working offline.
const CACHE = "verba-shell-v3";
const SHELL = [
  "/",
  "/styles.css",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/i18n/de.json",
  "/i18n/en.json",
  "/i18n/ru.json",
  "/js/api.js",
  "/js/app.js",
  "/js/dom.js",
  "/js/embeddings.js",
  "/js/export-dialog.js",
  "/js/hardware.js",
  "/js/i18n.js",
  "/js/icons.js",
  "/js/jobs.js",
  "/js/languages.js",
  "/js/markdown.js",
  "/js/ws.js",
  "/js/views/dashboard.js",
  "/js/views/docs.js",
  "/js/views/editor.js",
  "/js/views/login.js",
  "/js/views/project.js",
  "/js/views/search.js",
  "/js/views/settings.js",
  "/js/views/setup.js",
  "/js/views/types.js",
  "/js/views/users.js",
  "/vendor/marked.esm.js",
  "/vendor/wavesurfer.esm.js",
  "/vendor/wavesurfer.regions.esm.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (
    event.request.method !== "GET" ||
    url.origin !== location.origin ||
    url.pathname.startsWith("/api") ||
    url.pathname.startsWith("/v1") ||
    url.pathname === "/ws"
  ) {
    return; // network only
  }
  // network first, cache fallback (so updates arrive without cache busting);
  // offline navigations fall back to the cached shell so deep links keep working
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        if (event.request.mode === "navigate") {
          const shell = await caches.match("/");
          if (shell) return shell;
        }
        return Response.error();
      })
  );
});
