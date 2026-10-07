// Keeps the dashboard shell available even when the device process is down or
// the phone has no link to it. Only the shell is cached: every /api/ call goes
// to the device each time, so no note text is ever stored by the browser.
"use strict";

const VERSION = "fieldmind-shell-v3";
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

  // Shell files: network first, so an updated dashboard shows up on the next
  // load; the cached copy is only for when the device cannot be reached.
  event.respondWith(caches.open(VERSION).then(async (cache) => {
    try {
      const response = await fetch(event.request, { cache: "no-cache" });
      if (response.ok) cache.put(event.request, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(event.request, { ignoreSearch: url.pathname === "/" });
      return cached || Response.error();
    }
  }));
});
