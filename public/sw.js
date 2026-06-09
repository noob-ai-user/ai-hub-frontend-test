/* AI Hub root service worker — clears stale PWA caches from subpath apps ONLY.
 * SillyTavern (root path "/") is NEVER intercepted — this SW acts as a no-op
 * for root pages to prevent black-screen issues on Chrome incognito.
 *
 * Subpath apps (Lumiverse, Marinara) register their own PWA scopes —
 * this root SW only cleans up orphaned caches.
 */

const CACHE_NAME = "ai-hub-v4";
const NO_CACHE_PATHS = ["/hub", "/hub/", "/api/hub", "/api/hub/", "/hub.html",
                        "/switching.html", "/api/switch/", "/api/sync",
                        "/api/ready", "/api/active", "/api/debug"];

self.addEventListener("install", (event) => {
  // Do NOT skipWaiting — let the SW activate on the *next* navigation.
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Only act when subpath app clients exist — never claim root (ST) pages.
      const allClients = await self.clients.matchAll({ type: "window" });
      const hasSubpathClient = allClients.some(client => {
        try {
          const url = new URL(client.url);
          return url.pathname.startsWith("/apps/");
        } catch (e) { return false; }
      });
      if (hasSubpathClient) {
        const keys = await caches.keys();
        await Promise.all(keys.map((key) => caches.delete(key)));
      }
      // NEVER claim clients — in Chrome incognito this causes black screen.
      // await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const path = url.pathname;

  // Root paths (ST) — pass through, never intercept.
  if (!path.startsWith("/apps/") && !NO_CACHE_PATHS.some((p) => path === p || path.startsWith(p))) {
    // ST and other root paths: no interception
    return;
  }

  // Hub pages and API endpoints — never cache
  if (NO_CACHE_PATHS.some((p) => path === p || path.startsWith(p))) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Subpath app assets — network-first, no offline fallback
  event.respondWith(
    fetch(event.request).catch(() => {
      return new Response("Offline - AI Hub", {
        status: 503,
        headers: { "Content-Type": "text/plain" },
      });
    })
  );
});
