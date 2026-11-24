// Service worker minimal : rend l'app installable, ne met rien en cache (les données restent fraîches).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
