/* prisaMove service worker: offline-capable shell for low-connectivity regions. */
const CACHE = "prisamove-v1";
const SHELL = ["index.html", "marketplace.html", "tracking.html", "admin.html", "ops.html", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // API calls: network-first, fall back to a clear offline JSON.
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({ error: { code: "offline", message: "offline" } }),
          { status: 503, headers: { "Content-Type": "application/json" } }))
    );
    return;
  }
  // App shell: cache-first.
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
