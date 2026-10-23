self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open('trustcircle-v1').then((cache) => {
      return cache.addAll([
        '/',
        '/login',
        '/dashboard',
        '/static/manifest.json',
        '/static/icon.svg'
      ]);
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).then((response) => {
        const cloned = response.clone();
        caches.open('trustcircle-v1').then((cache) => {
          cache.put(event.request, cloned);
        });
        return response;
      }).catch(() => {
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
        return caches.match('/static/icon.svg');
      });
    })
  );
});
