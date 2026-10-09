/* Scarecrow Watch service worker — offline app shell + fresh data */
const VERSION = '2026-10-09-6';
const SHELL = 'sw-shell-' + VERSION;
const RUNTIME = 'sw-runtime-' + VERSION;

const SHELL_ASSETS = [
  './',
  './index.html',
  './about.html',
  './prevent.html',
  './assets/i18n.js',
  './assets/logo-icon.png',
  './assets/logo-full.png',
  './assets/icons/icon-192.png',
  './assets/icons/icon-512.png',
  './manifest.webmanifest'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => Promise.allSettled(SHELL_ASSETS.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== RUNTIME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => { if (e.data === 'skipWaiting') self.skipWaiting(); });

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // CDN, fonts, map tiles -> straight to network

  // Daily data: network-first so the app shows fresh numbers, cache as offline fallback
  if (url.pathname.includes('/data/')) {
    e.respondWith(
      fetch(req).then((res) => { const copy = res.clone(); caches.open(RUNTIME).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Page navigations: network-first, fall back to cached page then the shell
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then((res) => { const copy = res.clone(); caches.open(RUNTIME).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req).then((r) => r || caches.match('./index.html')))
    );
    return;
  }

  // Everything else same-origin (scripts, images, manifest): cache-first, then network
  e.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((res) => {
      const copy = res.clone(); caches.open(RUNTIME).then((c) => c.put(req, copy)); return res;
    }).catch(() => cached))
  );
});
