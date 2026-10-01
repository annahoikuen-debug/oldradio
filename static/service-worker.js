// Service Worker for Retro Radio Time Machine PWA
// - /api/ と別オリジン（iTunes プレビュー等）は必ずネットワークへ素通しする
// - HTML / JS / CSS は network-first（開発中に古いキャッシュが応答しない）
// - install は 1 件の失敗で全体が落ちないよう個別 add + catch を行う
var CACHE_NAME = 'retro-radio-cache-v3';
var OFFLINE_URL = '/static/offline.html';
// app.js を入れないと、オフライン起動時に HTML の殻だけが残り
// ボタンが一切効かない「だけの画面」になる。
var STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  OFFLINE_URL,
  '/static/app.css',
  '/static/app.js'
];
var NETWORK_FIRST_EXTENSIONS = ['.html', '.css', '.js', '.json', '.webmanifest', '.mjs'];

// Install event - 静的アセットを個別にキャッシュする
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) {
        // 1 つ失敗しても install 全体は失敗させない
        return Promise.all(STATIC_ASSETS.map(function (url) {
          return cache.add(url).catch(function () { return undefined; });
        }));
      })
      .then(function () { return self.skipWaiting(); })
  );
});

// Activate event - 旧バージョンのキャッシュを削除して即時反映
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (cacheNames) {
        return Promise.all(cacheNames
          .filter(function (cacheName) { return cacheName !== CACHE_NAME; })
          .map(function (cacheName) { return caches.delete(cacheName); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function isNetworkFirstAsset(pathname) {
  if (pathname.indexOf('/static/') === 0) {
    return true;
  }
  return NETWORK_FIRST_EXTENSIONS.some(function (ext) {
    return pathname.toLowerCase().endsWith(ext);
  });
}

function putInCache(request, response) {
  if (!response || !response.ok || response.type === 'opaque') {
    return;
  }
  var copy = response.clone();
  caches.open(CACHE_NAME)
    .then(function (cache) { return cache.put(request, copy); })
    .catch(function () { return undefined; });
}

function networkFirst(request) {
  return fetch(request)
    .then(function (response) {
      putInCache(request, response);
      return response;
    })
    .catch(function () {
      return caches.match(request).then(function (cached) {
        return cached || Response.error();
      });
    });
}

function cacheFirst(request) {
  return caches.match(request).then(function (cached) {
    if (cached) {
      return cached;
    }
    return fetch(request)
      .then(function (response) {
        putInCache(request, response);
        return response;
      })
      .catch(function () { return Response.error(); });
  });
}

function handleNavigation(request) {
  return fetch(request)
    .then(function (response) {
      putInCache(request, response);
      return response;
    })
    .catch(function () {
      return caches.match(request)
        .then(function (cached) { return cached || caches.match('/'); })
        .then(function (cached) { return cached || caches.match(OFFLINE_URL); })
        .then(function (cached) { return cached || Response.error(); });
    });
}

// Fetch event
self.addEventListener('fetch', function (event) {
  var request = event.request;

  // POST /api/generate 等は respondWith もキャッシュもしない
  if (request.method !== 'GET') {
    return;
  }

  var url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }

  // 別オリジン（Google Fonts / iTunes プレビュー等）はブラウザに任せる
  if (url.origin !== self.location.origin) {
    return;
  }

  // 会話 API と音声ファイル（Range 対応が不安定）は必ずネットワーク
  if (url.pathname.indexOf('/api/') === 0) {
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(handleNavigation(request));
    return;
  }

  if (isNetworkFirstAsset(url.pathname)) {
    event.respondWith(networkFirst(request));
    return;
  }

  event.respondWith(cacheFirst(request));
});
