// 아주 단순한 서비스 워커입니다.
// 오프라인 캐싱 기능은 없고, "홈 화면에 추가"했을 때 브라우저 주소창 없이
// 진짜 앱처럼(풀스크린) 열리도록 하기 위한 최소 요건(fetch 핸들러 등록)만 채웁니다.
self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  event.respondWith(fetch(event.request));
});

