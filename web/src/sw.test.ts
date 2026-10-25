import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { describe, expect, it, vi } from "vitest";

/**
 * The service worker (public/sw.js) against a fake worker global: what a push shows, where a tap on it goes, and how a
 * replaced subscription reaches the hub. Real pushes need a push service, which the end-to-end tests do not have.
 */
const SOURCE = readFileSync(fileURLToPath(new URL("../public/sw.js", import.meta.url)), "utf8");
const ORIGIN = "https://hub.example.test";

type Listener = (event: Record<string, unknown>) => void;
type FakeWindow = { url: string; focus: () => Promise<FakeWindow>; navigate?: (url: string) => Promise<FakeWindow | null> };

function tab(url: string, { controlled = true }: { controlled?: boolean } = {}): FakeWindow {
  const window: FakeWindow = {
    url,
    focus: vi.fn(async () => window),
    navigate: vi.fn(async (next: string) => {
      if (!controlled) throw new TypeError("not controlled by this worker");
      window.url = next;
      return window;
    }),
  };
  return window;
}

function json(body: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function startWorker({ windows = [], fetch = vi.fn() }: { windows?: FakeWindow[]; fetch?: ReturnType<typeof vi.fn> } = {}) {
  const listeners = new Map<string, Listener>();
  const subscription = { toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/new", keys: { p256dh: "p", auth: "a" } }) };
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
    skipWaiting: vi.fn(),
    clients: {
      claim: vi.fn(async () => undefined),
      matchAll: vi.fn(async () => windows),
      openWindow: vi.fn(async () => null),
    },
    registration: {
      showNotification: vi.fn(async () => undefined),
      pushManager: { subscribe: vi.fn(async () => subscription) },
    },
  };
  vm.runInNewContext(SOURCE, { self, fetch, atob, URL, Uint8Array, JSON });
  const dispatch = async (type: string, init: Record<string, unknown> = {}) => {
    const waits: Promise<unknown>[] = [];
    listeners.get(type)!({ ...init, waitUntil: (pending: Promise<unknown>) => waits.push(pending) });
    await Promise.all(waits);
  };
  return { self, dispatch, fetch };
}

const data = (payload: unknown) => ({ json: () => payload });

describe("the service worker", () => {
  it("shows a push as the hub wrote it, tagged by its notification, opening on this origin", async () => {
    const { self, dispatch } = startWorker();
    await dispatch("push", {
      data: data({ title: "Decision needed", body: "Deploy now?\nRecommended: Deploy", url: `${ORIGIN}/inbox?decision=7`, tag: "evo-notification-7", kind: "decision", project: "northwind" }),
    });
    expect(self.registration.showNotification).toHaveBeenCalledWith("Decision needed", {
      body: "Deploy now?\nRecommended: Deploy",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: "evo-notification-7",
      data: { url: `${ORIGIN}/inbox?decision=7`, kind: "decision", project: "northwind" },
    });
  });

  it("keeps a link of another origin on its own, and shows a push it cannot read all the same", async () => {
    const { self, dispatch } = startWorker();
    await dispatch("push", { data: data({ title: "Run failed", url: "https://elsewhere.example/p/lumen/runs/9#trace" }) });
    expect(self.registration.showNotification).toHaveBeenLastCalledWith("Run failed", expect.objectContaining({ data: expect.objectContaining({ url: `${ORIGIN}/p/lumen/runs/9#trace` }) }));
    await dispatch("push", { data: data({ title: "x", url: "javascript:alert(1)" }) });
    expect(self.registration.showNotification).toHaveBeenLastCalledWith("x", expect.objectContaining({ data: expect.objectContaining({ url: `${ORIGIN}/inbox` }) }));
    await dispatch("push", { data: { json: () => { throw new SyntaxError("not JSON"); } } });
    expect(self.registration.showNotification).toHaveBeenLastCalledWith("Evo Agent Hub", expect.objectContaining({ body: "", data: expect.objectContaining({ url: `${ORIGIN}/inbox` }) }));
    const options = (self.registration.showNotification.mock.lastCall as unknown as [string, Record<string, unknown>])[1];
    expect(options).not.toHaveProperty("tag");
  });

  it("focuses the tab already on the page a tap opens", async () => {
    const onPage = tab(`${ORIGIN}/inbox?decision=7`);
    const other = tab(`${ORIGIN}/`);
    const { self, dispatch } = startWorker({ windows: [other, onPage] });
    const close = vi.fn();
    await dispatch("notificationclick", { notification: { close, data: { url: `${ORIGIN}/inbox?decision=7` } } });
    expect(close).toHaveBeenCalled();
    expect(onPage.focus).toHaveBeenCalled();
    expect(other.focus).not.toHaveBeenCalled();
    expect(self.clients.openWindow).not.toHaveBeenCalled();
  });

  it("takes the hub's open tab to the page, or opens one when there is none or it cannot move", async () => {
    const home = tab(`${ORIGIN}/`);
    const first = startWorker({ windows: [home] });
    await first.dispatch("notificationclick", { notification: { close: vi.fn(), data: { url: `${ORIGIN}/p/lumen/runs/9` } } });
    expect(home.focus).toHaveBeenCalled();
    expect(home.navigate).toHaveBeenCalledWith(`${ORIGIN}/p/lumen/runs/9`);
    expect(first.self.clients.openWindow).not.toHaveBeenCalled();

    const none = startWorker();
    await none.dispatch("notificationclick", { notification: { close: vi.fn(), data: { url: `${ORIGIN}/` } } });
    expect(none.self.clients.openWindow).toHaveBeenCalledWith(`${ORIGIN}/`);

    const stuck = startWorker({ windows: [tab(`${ORIGIN}/monitor`, { controlled: false })] });
    await stuck.dispatch("notificationclick", { notification: { close: vi.fn(), data: {} } });
    expect(stuck.self.clients.openWindow).toHaveBeenCalledWith(`${ORIGIN}/inbox`);
  });

  it("subscribes again with the hub's key when the browser replaced the subscription, and tells the hub", async () => {
    const fetch = vi.fn(async (url: string, _init?: unknown) => {
      if (url === "/v1/push/config") return json({ vapid_public_key: "AQID", senders: { webpush: true } });
      if (url === "/v1/auth/web/csrf") return json({ csrf: "token-1", header: "X-Evo-CSRF" });
      return json({ id: 4 }, 201);
    });
    const { self, dispatch } = startWorker({ fetch });
    await dispatch("pushsubscriptionchange", { oldSubscription: null, newSubscription: null });
    const [{ applicationServerKey, userVisibleOnly }] = self.registration.pushManager.subscribe.mock.lastCall as unknown as [{ applicationServerKey: Uint8Array; userVisibleOnly: boolean }];
    expect(userVisibleOnly).toBe(true);
    expect([...applicationServerKey]).toEqual([1, 2, 3]);
    const post = fetch.mock.calls.find(([url]) => url === "/v1/me/push/devices");
    expect(post).toBeDefined();
    const init = post![1] as unknown as { method: string; headers: Record<string, string>; body: string };
    expect(init.method).toBe("POST");
    expect(init.headers["X-Evo-CSRF"]).toBe("token-1");
    expect(JSON.parse(init.body)).toEqual({ kind: "webpush", subscription: { endpoint: "https://fcm.googleapis.com/fcm/send/new", keys: { p256dh: "p", auth: "a" } } });
  });

  it("sends nothing when the browser is signed out or push is off on the hub", async () => {
    const signedOut = vi.fn(async (url: string) => (url === "/v1/auth/web/csrf" ? json({ detail: "sign in" }, 401) : json({ vapid_public_key: "AQID" })));
    const out = startWorker({ fetch: signedOut });
    await out.dispatch("pushsubscriptionchange", { oldSubscription: { options: { applicationServerKey: new Uint8Array([9]) } } });
    expect(signedOut.mock.calls.map(([url]) => url)).toEqual(["/v1/auth/web/csrf"]);

    const off = vi.fn(async (_url: string) => json({ vapid_public_key: null, senders: { webpush: false } }));
    const quiet = startWorker({ fetch: off });
    await quiet.dispatch("pushsubscriptionchange", {});
    expect(quiet.self.registration.pushManager.subscribe).not.toHaveBeenCalled();
    expect(off.mock.calls.map(([url]) => url)).toEqual(["/v1/push/config"]);
  });

  it("takes control of the pages at once, and has no fetch handler", async () => {
    const { self, dispatch } = startWorker();
    await dispatch("install");
    await dispatch("activate");
    expect(self.skipWaiting).toHaveBeenCalled();
    expect(self.clients.claim).toHaveBeenCalled();
    expect(SOURCE).not.toMatch(/addEventListener\(\s*["']fetch["']/);
  });
});
