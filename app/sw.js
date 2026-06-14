// Service Worker — network-first for app files, cache for offline fallback
const CACHE = 'ssm-v12';

// 只预缓存真正稳定的外壳资源
const PRECACHE = [
    '/manifest.json',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE).then(c => c.addAll(PRECACHE)).catch(() => {})
    );
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
          .then(() => self.clients.matchAll({ type: 'window' }))
          .then(clients => clients.forEach(c => c.postMessage({ type: 'SW_UPDATED' })))
    );
});

self.addEventListener('fetch', e => {
    // CDN 资源（外部域名）直接透传，不干预
    if (!e.request.url.startsWith(self.location.origin)) return;

    // 所有同源请求：先网络，失败再用缓存
    e.respondWith(
        fetch(new Request(e.request, { cache: 'no-store' }))
            .then(res => {
                // 成功拿到网络响应，顺手更新缓存（离线兜底用）
                if (res && res.status === 200 && res.type === 'basic') {
                    const clone = res.clone();
                    caches.open(CACHE).then(c => c.put(e.request, clone));
                }
                return res;
            })
            .catch(() => caches.match(e.request))
    );
});
