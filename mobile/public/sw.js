// GuardRails service worker — network-first for the app shell and API data so a
// fresh deploy is picked up immediately, with a cache fallback for offline use.
// Hashed build assets (/assets/*) are immutable, so those stay cache-first.
const CACHE = "guardrails-v4";
const APP_SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

function cachePut(request, response) {
  if (!response || !response.ok) return response;
  const copy = response.clone();
  caches.open(CACHE).then((cache) => cache.put(request, copy));
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  const path = url.pathname;

  // Cross-origin — let the browser decide.
  if (url.origin !== self.location.origin) return;

  // Immutable, content-hashed build assets: cache first.
  if (path.startsWith("/assets/")) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((res) => cachePut(request, res))
      )
    );
    return;
  }

  // API data and the app shell: network first so deploys and live account data
  // are never masked by a stale cache; fall back to the cache when offline.
  event.respondWith(
    fetch(request)
      .then((res) => cachePut(request, res))
      .catch(() => caches.match(request))
  );
});
