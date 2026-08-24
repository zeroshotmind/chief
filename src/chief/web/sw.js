/* The service worker exists to make Chief installable and to survive a dropped connection —
   not to make it fast. It is deliberately the least clever cache that satisfies that.

   **Network first, always.** A cache-first shell would serve yesterday's `app.js` after an
   edit, which on a tool you are actively developing is a bug that looks like a mystery. The
   cache is a fallback for when the tailnet is not reachable, nothing more.

   **`/v1` is never cached, not even as a fallback.** Stale run state is worse than no run
   state: a cached "waiting on you" for a checkpoint already decided, or a run shown as
   running long after it failed, is a lie told confidently. Offline, the API calls fail and
   the UI says so — which is true. */

const CACHE = "chief-shell-v1";

// The shell, not the data. Enough to boot the UI and render its own error when /v1 is
// unreachable.
const SHELL = [
  "./", "./index.html", "./app.js", "./api.js", "./chief.css",
  "./markdown.js", "./jsx.js", "./mdx-runtime.js",
  "./chief-mark.svg", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png", "./icon-maskable-512.png", "./apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  // `addAll` would fail the whole install on one 404; the shell list is maintained by hand
  // and a missing entry should degrade, not brick the worker.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return; // reports and decisions are never replayed from a cache

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The API and the MCP mount go to the network or they fail. See the note above.
  if (url.pathname.startsWith("/v1") || url.pathname.startsWith("/mcp")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("./index.html"))),
  );
});
