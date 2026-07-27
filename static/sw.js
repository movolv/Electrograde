// Minimal service worker: caches the static app-shell assets (icons, manifest)
// so the "installed" PWA has an icon/name available offline. Streamlit's UI
// itself is a live websocket app and is NOT cached here — this app requires
// a network connection to function, it just *launches* like a native app.
const CACHE_NAME = "electro-grader-shell-v1";
const SHELL_ASSETS = [
  "app/static/manifest.json",
  "app/static/icons/icon-192.png",
  "app/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Only serve cached shell assets from cache; everything else (the live
  // Streamlit app/websocket) always goes to the network.
  const url = new URL(event.request.url);
  if (SHELL_ASSETS.some((a) => url.pathname.endsWith(a))) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
