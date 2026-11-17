/* Offline shell for the installed app. Network first, so a new version shows up on the next online start;
   the cached copy is used only when the network cannot be reached. Requests to other hosts are left alone. */
const CACHE = 'expense-shell-v1';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'lib/jspdf.umd.min.js', 'lib/jspdf.plugin.autotable.min.js', 'lib/xlsx.mini.min.js', 'icons/icon-192.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || new URL(e.request.url).origin !== self.location.origin) return;
  e.respondWith(fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); } return r; })
    .catch(() => caches.match(e.request, {ignoreSearch: true}).then(r => r || caches.match('index.html'))));
});
