// Service Worker — cache PWA shell for offline use
const CACHE = 'ssm-v1';
const ASSETS = [
    '/',
    '/index.html',
    '/styles/app.css',
    '/src/MqttBus.js',
    '/src/AgentRegistry.js',
    '/src/ISMTracker.js',
    '/src/DecisionAgent.js',
    '/src/ui/Dashboard.js',
    '/src/ui/ManualControl.js',
    '/src/ui/EventLog.js',
    '/src/main.js',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(() => {})
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
    // Only cache same-origin requests; pass CDN (mqtt.js) through
    if (!e.request.url.startsWith(self.location.origin)) return;
    e.respondWith(
        caches.match(e.request).then(r => r || fetch(e.request))
    );
});
