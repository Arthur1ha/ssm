// Service Worker — network-first for app files, cache for offline fallback
const CACHE = 'ssm-v10';

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
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', e => {
    // CDN 资源（外部域名）直接透传，不干预
    if (!e.request.url.startsWith(self.location.origin)) return;

    // 所有同源请求：先网络，失败再用缓存
    e.respondWith(
        fetch(e.request)
            .then(res => {
                // 成功拿到网络响应，顺手更新缓存
                if (res && res.status === 200 && res.type === 'basic') {
                    const clone = res.clone();
                    caches.open(CACHE).then(c => c.put(e.request, clone));
                }
                return res;
            })
            .catch(() => caches.match(e.request))
    );
});
