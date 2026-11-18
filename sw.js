const CACHE = 'bank-report-v3';
const CACHE_PREFIX = 'bank-report-';
const BASE_URL = new URL('./', self.location.href);
const OFFLINE_URL = new URL('index.html', BASE_URL).href;
const ICON_URLS = ['icons/icon-192.png', 'icons/icon-512.png'].map(path => new URL(path, BASE_URL).href);
const FRESH_PATHS = ['latest.json', 'manifest.webmanifest', 'sw.js'].map(path => new URL(path, BASE_URL).pathname);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // A missing optional icon must not prevent the update mechanism from installing.
    await Promise.all(ICON_URLS.map(async url => {
      try {
        const response = await fetch(url, {cache: 'reload'});
        if (response.ok) await cache.put(url, response);
      } catch (_) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    const cache = await caches.open(CACHE);
    // Preserve the last readable report while migrating the old offline cache.
    if (!await cache.match(OFFLINE_URL)) {
      for (const key of keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE).reverse()) {
        const previous = await (await caches.open(key)).match(OFFLINE_URL);
        if (previous) {
          await cache.put(OFFLINE_URL, previous);
          break;
        }
      }
    }
    await Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

async function readReport(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request, {cache: 'no-store'});
    if (response.ok) {
      try { await cache.put(OFFLINE_URL, response.clone()); } catch (_) {}
      return response;
    }
    if (response.status >= 500) {
      const saved = await cache.match(OFFLINE_URL);
      if (saved) return saved;
    }
    return response;
  } catch (_) {
    const saved = await cache.match(OFFLINE_URL);
    return saved || new Response('当前离线，请联网后重新打开日报。', {
      status: 503,
      headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'}
    });
  }
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== BASE_URL.origin) return;
  // Never serve release metadata or the install manifest from Cache Storage.
  if (FRESH_PATHS.includes(url.pathname)) {
    event.respondWith(fetch(request, {cache: 'no-store'}));
    return;
  }
  if (request.mode === 'navigate' && (url.pathname === BASE_URL.pathname || url.href.split('?')[0] === OFFLINE_URL)) {
    event.respondWith(readReport(request));
    return;
  }
  if (ICON_URLS.includes(url.href.split('?')[0])) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      return await cache.match(url.href.split('?')[0]) || fetch(request);
    })());
  }
});
