// Keeps the dashboard shell available even when the device process is down or
// the phone has no link to it. Only the shell is cached: every /api/ call goes
// to the device each time, so no note text is ever stored by the browser.
"use strict";

const VERSION = "fieldmind-shell-v2";
const SHELL = ["/", "/static/styles.css", "/static/app.js", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/metrics" || url.pathname === "/healthz") return;

  // Shell files: answer from the cache at once, refresh it in the background.
  event.respondWith(caches.open(VERSION).then(async (cache) => {
    const cached = await cache.match(event.request, { ignoreSearch: url.pathname === "/" });
    const refresh = fetch(event.request).then((response) => {
      if (response.ok) cache.put(event.request, response.clone());
      return response;
    }).catch(() => cached);
    return cached || refresh;
  }));
});
