const CACHE = 'sangpum-v2';
const STATIC = ['/', '/index.html', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('push', e => {
  const data = e.data ? e.data.json() : { title: '상품조회', body: '새 알림' };
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      // 열린 앱 탭에 인앱 알림 메시지 전달
      list.forEach(c => c.postMessage({ type: 'PUSH_NOTIFY', title: data.title, body: data.body, url: data.url || '/', tag: data.tag || '' }));
      // OS 푸시 알림도 항상 표시
      return self.registration.showNotification(data.title, {
        body: data.body,
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        vibrate: [200, 100, 200, 100, 200],
        tag: data.tag || 'sangpum',
        renotify: true,
        requireInteraction: false,
        silent: false,
        data: { url: data.url || '/' }
      });
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  const roomMatch = url.match(/[?&]room=(\d+)/);
  const roomId = roomMatch ? roomMatch[1] : null;
  const viewMatch = url.match(/[?&]view=(\w+)/);
  const view = viewMatch ? viewMatch[1] : null;
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const existing = list.find(c => c.url.includes(self.location.origin));
      if (existing) {
        existing.focus();
        const idMatch = url.match(/[?&]id=(\d+)/);
        const itemId = idMatch ? idMatch[1] : null;
        if (roomId) existing.postMessage({ type: 'OPEN_CHAT_ROOM', roomId });
        else if (view === 'orders') existing.postMessage({ type: 'OPEN_ORDERS', id: itemId });
        else if (view === 'as') existing.postMessage({ type: 'OPEN_AS', id: itemId });
        else if (view === 'issues') existing.postMessage({ type: 'OPEN_ISSUES' });
        return;
      }
      return clients.openWindow(url);
    })
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API 요청은 항상 네트워크로
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', { headers: { 'Content-Type': 'application/json' } })));
    return;
  }
  // index.html은 항상 네트워크 우선 (업데이트 즉시 반영)
  if (url.pathname === '/' || url.pathname === '/index.html') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/index.html')));
    return;
  }
  // 나머지 정적 파일은 캐시 우선
  e.respondWith(caches.match(e.request).then(cached => cached || fetch(e.request)));
});
