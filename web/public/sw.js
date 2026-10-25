/*
 * The hub's service worker, at /sw.js with the scope of the whole web: it shows the hub's push notifications and opens
 * them, nothing else. It has no fetch handler and caches nothing, so every page loads as it would without it.
 *
 * - push: a payload as the hub's senders write it (evo_agents/hub/push_payload.py: title, body, url, tag, kind,
 *   project) becomes one notification. Its tag is the notification's, so a later push of the same notification
 *   replaces the earlier one. A push the worker cannot read still shows a notification, as browsers require.
 * - notificationclick: focus the hub's tab already on that page, or the hub's first tab and take it there, or open a
 *   new one. Only a page of the web's own origin opens: a URL of another origin keeps its path on this one.
 * - pushsubscriptionchange: the browser replaced the subscription; subscribe again with the hub's VAPID key and send the
 *   new one to POST /v1/me/push/devices with the session's cookie and CSRF header, as the settings page does.
 *
 * The proxy (src/proxy.ts) leaves this file out of its matcher, so the browser fetches it without a session.
 * src/sw.test.ts runs it against a fake worker global.
 */
"use strict";

const FALLBACK_TITLE = "Evo Agent Hub";
const FALLBACK_URL = "/inbox";
const ICON = "/icon-192.png";
const DEVICES = "/v1/me/push/devices";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

/** The payload of a push as an object, or null when it is not the hub's JSON. */
function readPayload(data) {
  if (!data) return null;
  try {
    const value = data.json();
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function text(value, fallback) {
  return typeof value === "string" && value.trim() ? value : fallback;
}

/** `url` as an absolute URL of this origin: a path, or a URL of another origin with its path kept here. */
function ownUrl(url) {
  const origin = self.location.origin;
  try {
    const parsed = new URL(typeof url === "string" && url ? url : FALLBACK_URL, origin);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return new URL(FALLBACK_URL, origin).href;
    return new URL(`${parsed.pathname}${parsed.search}${parsed.hash}`, origin).href;
  } catch {
    return new URL(FALLBACK_URL, origin).href;
  }
}

self.addEventListener("push", (event) => {
  const payload = readPayload(event.data) || {};
  const title = text(payload.title, FALLBACK_TITLE);
  const options = {
    body: text(payload.body, ""),
    icon: ICON,
    badge: ICON,
    data: { url: ownUrl(payload.url), kind: text(payload.kind, null), project: text(payload.project, null) },
  };
  if (typeof payload.tag === "string" && payload.tag) options.tag = payload.tag;
  event.waitUntil(self.registration.showNotification(title, options));
});

/** Focus the hub's tab on `url`, or its first tab taken to `url`, or a new tab. */
async function openPage(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const same = windows.find((client) => client.url === url);
  if (same) return same.focus();
  const first = windows.find((client) => client.url.startsWith(self.location.origin));
  if (first) {
    try {
      const focused = await first.focus();
      const moved = typeof focused.navigate === "function" ? await focused.navigate(url) : null;
      if (moved) return moved;
    } catch {
      // A tab this worker does not control cannot be navigated: a new one opens below.
    }
  }
  return self.clients.openWindow(url);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil(openPage(ownUrl(data.url)));
});

/** The hub's VAPID key as bytes, from the old subscription when it says, else from GET /v1/push/config. */
async function serverKey(old) {
  const known = old && old.options && old.options.applicationServerKey;
  if (known) return known;
  const response = await fetch("/v1/push/config", { credentials: "same-origin" });
  if (!response.ok) return null;
  const config = await response.json();
  const key = config && config.vapid_public_key;
  if (typeof key !== "string" || !key) return null;
  const padded = key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (key.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
}

/** Subscribe again and tell the hub; a visitor signed out of this browser (401) or a hub with push off stops here. */
async function resubscribe(event) {
  const subscription =
    event.newSubscription ||
    (await (async () => {
      const key = await serverKey(event.oldSubscription);
      if (!key) return null;
      return self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    })());
  if (!subscription) return;
  const csrf = await fetch("/v1/auth/web/csrf", { credentials: "same-origin" });
  if (!csrf.ok) return;
  const { csrf: token, header } = await csrf.json();
  await fetch(DEVICES, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json", [header]: token },
    body: JSON.stringify({ kind: "webpush", subscription: subscription.toJSON() }),
  });
}

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(resubscribe(event).catch(() => undefined));
});
