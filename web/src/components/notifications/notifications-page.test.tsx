// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast as sonner } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { NotificationSettings, PushConfig, PushDevice } from "@/lib/notification-queries";
import { renderVi } from "@/test/render";

import { NotificationsPage } from "./notifications-page";

type Seen = { method: string; path: string; body: unknown };

const api = vi.hoisted(() => ({
  settings: null as unknown as NotificationSettings,
  config: null as unknown as PushConfig,
  devices: [] as PushDevice[],
  seen: [] as Seen[],
  put: null as null | ((body: Record<string, unknown>) => Response),
  test: null as null | (() => Response),
  add: null as null | (() => Response),
  nextId: 30,
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        const text = await request.clone().text();
        const body = text ? JSON.parse(text) : null;
        if (path === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        api.seen.push({ method: request.method, path, body });
        if (path === "/v1/me/notification-settings" && request.method === "GET") return Response.json(api.settings);
        if (path === "/v1/me/notification-settings" && request.method === "PUT") {
          if (api.put) return api.put(body);
          api.settings = { ...api.settings, ...(body.hide_details !== undefined ? { hide_details: body.hide_details } : {}) };
          return Response.json(api.settings);
        }
        if (path === "/v1/push/config") return Response.json(api.config);
        if (path === "/v1/me/push/devices" && request.method === "GET") return Response.json({ devices: api.devices });
        if (path === "/v1/me/push/devices" && request.method === "POST") {
          if (api.add) return api.add();
          const device: PushDevice = { id: api.nextId++, kind: "webpush", label: body.label, created_at: "2026-10-25T03:00:00Z", last_sent_at: null, this_session: true };
          api.devices = [device, ...api.devices];
          return Response.json(device, { status: 201 });
        }
        const test = /^\/v1\/me\/push\/devices\/(\d+)\/test$/.exec(path);
        if (test) return api.test ? api.test() : Response.json({ sent: true, removed: false, error: null });
        const one = /^\/v1\/me\/push\/devices\/(\d+)$/.exec(path);
        if (one && request.method === "DELETE") {
          api.devices = api.devices.filter((device) => device.id !== Number(one[1]));
          return new Response(null, { status: 204 });
        }
        return Response.json({ error: "not_found", message: "no route" }, { status: 404 });
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/settings/notifications" }));

const KEY = "BAEC_-8";
const ON: PushConfig = { vapid_public_key: KEY, senders: { webpush: true, apns: false, fcm: false } };
const OFF: PushConfig = { vapid_public_key: null, senders: { webpush: false, apns: false, fcm: false } };
const CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";
const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const OWN_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const HUB_ZONE = OWN_ZONE === "Pacific/Chatham" ? "Pacific/Apia" : "Pacific/Chatham";

const KINDS = ["decision", "proposal", "push_default_branch", "run_failed", "curator_brief", "merge_paused", "release", "run_outputs", "today"] as const;
const DEFAULTS = ["decision", "run_failed", "release", "merge_paused"];

function settings(extra: Partial<NotificationSettings> = {}): NotificationSettings {
  return {
    notices: [{ kind: "run_outputs", on: true }],
    hide_details: false,
    tiers: KINDS.map((kind) => ({ kind, tier: DEFAULTS.includes(kind) ? "interrupt" : "brief", chosen: false })),
    quiet_hours: null,
    time_zone: HUB_ZONE,
    time_zone_chosen: false,
    digest_at: "07:30",
    ...extra,
  };
}

const LAPTOP: PushDevice = { id: 4, kind: "webpush", label: "Firefox on Linux", created_at: "2026-10-20T03:00:00Z", last_sent_at: "2026-10-24T03:00:00Z", this_session: false };

/** A browser as the test says: its user agent, what it has of push, and a push manager that subscribes. */
function browser({ userAgent = CHROME, push = true, permission = "default" as NotificationPermission, grant = "granted" as NotificationPermission } = {}) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
  const subscription = {
    endpoint: "https://fcm.googleapis.com/fcm/send/this-browser",
    options: { applicationServerKey: null },
    toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/this-browser", keys: { p256dh: "BPk", auth: "c2Vj" } }),
    unsubscribe: vi.fn(async () => true),
  };
  let held: typeof subscription | null = null;
  const pushManager = {
    getSubscription: vi.fn(async () => held),
    subscribe: vi.fn(async () => {
      held = subscription;
      return subscription;
    }),
  };
  const registration = { pushManager };
  const serviceWorker = {
    register: vi.fn(async () => registration),
    ready: Promise.resolve(registration),
    getRegistration: vi.fn(async () => (held ? registration : undefined)),
  };
  const requestPermission = vi.fn(async () => {
    notification.permission = grant;
    return grant;
  });
  const notification = { permission, requestPermission };
  if (push) {
    Object.defineProperty(navigator, "serviceWorker", { value: serviceWorker, configurable: true });
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("Notification", notification);
  }
  return { serviceWorker, pushManager, subscription, requestPermission };
}

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <NotificationsPage initialError={null} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

function writes() {
  return api.seen.filter((request) => request.method !== "GET");
}

beforeEach(() => {
  api.settings = settings();
  api.config = ON;
  api.devices = [];
  api.seen = [];
  api.put = null;
  api.test = null;
  api.add = null;
  api.nextId = 30;
  sonner.dismiss();
  window.localStorage.clear();
  // Radix's switch measures itself, which jsdom cannot.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("push on this browser", () => {
  it("turns push on from one click: asks permission, subscribes with the hub's key and adds this browser's device", async () => {
    const fake = browser();
    const user = setup();
    const card = await screen.findByTestId("push-browser");
    expect(card).toHaveAttribute("data-state", "off");
    expect(fake.requestPermission).not.toHaveBeenCalled();
    await user.click(within(card).getByRole("button", { name: "Bật push cho trình duyệt này" }));

    await waitFor(() => expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "on"));
    expect(fake.requestPermission).toHaveBeenCalledTimes(1);
    expect(fake.serviceWorker.register).toHaveBeenCalledWith("/sw.js", { scope: "/" });
    const [{ applicationServerKey, userVisibleOnly }] = fake.pushManager.subscribe.mock.calls[0] as unknown as [{ applicationServerKey: Uint8Array; userVisibleOnly: boolean }];
    expect(userVisibleOnly).toBe(true);
    expect([...applicationServerKey]).toEqual([4, 1, 2, 255, 239]);
    expect(writes()).toEqual([
      {
        method: "POST",
        path: "/v1/me/push/devices",
        body: { kind: "webpush", label: "Chrome on macOS", subscription: { endpoint: "https://fcm.googleapis.com/fcm/send/this-browser", keys: { p256dh: "BPk", auth: "c2Vj" } } },
      },
    ]);
    expect(JSON.parse(window.localStorage.getItem("evo-push-device")!)).toEqual({ id: 30, endpoint: "https://fcm.googleapis.com/fcm/send/this-browser" });
    const row = await screen.findByTestId("push-device");
    expect(within(row).getByTestId("push-device-here")).toHaveTextContent("Trình duyệt này");
    expect(await screen.findByText("Đã bật push cho trình duyệt này")).toBeInTheDocument();
  });

  it("turns push off: removes the device and ends the subscription", async () => {
    const fake = browser({ permission: "granted" });
    await fake.pushManager.subscribe();
    api.devices = [{ ...LAPTOP, id: 9, label: "Chrome on macOS", this_session: true }];
    window.localStorage.setItem("evo-push-device", JSON.stringify({ id: 9, endpoint: fake.subscription.endpoint }));
    const user = setup();
    await waitFor(() => expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "on"));
    await user.click(screen.getByTestId("push-browser-off"));
    await waitFor(() => expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "off"));
    expect(writes()).toEqual([{ method: "DELETE", path: "/v1/me/push/devices/9", body: null }]);
    expect(fake.subscription.unsubscribe).toHaveBeenCalled();
    expect(window.localStorage.getItem("evo-push-device")).toBeNull();
  });

  it("sends a subscription the browser replaced to the hub as soon as the page reads it", async () => {
    const fake = browser({ permission: "granted" });
    await fake.pushManager.subscribe();
    api.devices = [{ ...LAPTOP, id: 9 }];
    window.localStorage.setItem("evo-push-device", JSON.stringify({ id: 9, endpoint: "https://fcm.googleapis.com/fcm/send/old" }));
    setup();
    await waitFor(() => expect(writes().map((request) => `${request.method} ${request.path}`)).toEqual(["POST /v1/me/push/devices"]));
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem("evo-push-device")!)).toEqual({ id: 30, endpoint: fake.subscription.endpoint }));
    await waitFor(() => expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "on"));
  });

  it("says when notifications are blocked for the site, and offers no button", async () => {
    browser({ permission: "denied" });
    setup();
    expect(await screen.findByTestId("push-browser")).toHaveAttribute("data-state", "denied");
    expect(screen.queryByTestId("push-browser-on")).toBeNull();
  });

  it("asks again when the prompt was dismissed, and shows why a subscription failed", async () => {
    const fake = browser({ grant: "default" });
    const user = setup();
    await user.click(await screen.findByTestId("push-browser-on"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Trình duyệt chưa cho phép thông báo");
    expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "off");

    fake.requestPermission.mockResolvedValue("granted");
    fake.pushManager.subscribe.mockRejectedValueOnce(new DOMException("push service not available", "AbortError"));
    await user.click(screen.getByTestId("push-browser-on"));
    const alert = await screen.findByText("Không bật được push");
    expect(alert.closest("[role=alert]")).toHaveTextContent("Trình duyệt không đăng ký được: push service not available");
    expect(writes()).toEqual([]);
  });

  it("refuses a device over the hub's limit with what to do, beside the button", async () => {
    browser();
    api.add = () => Response.json({ error: "conflict", message: "you have 20 push devices already: remove one before adding another" }, { status: 409 });
    const user = setup();
    await user.click(await screen.findByTestId("push-browser-on"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Bạn đã có đủ số thiết bị hub giữ. Hãy gỡ một thiết bị bên dưới rồi thử lại.");
    expect(screen.getByTestId("push-browser")).toHaveAttribute("data-state", "off");
    expect(window.localStorage.getItem("evo-push-device")).toBeNull();
  });

  it("shows how to add the hub to the Home Screen on an iPhone's Safari, and that push is off on a hub without a key", async () => {
    browser({ userAgent: IPHONE, push: false });
    setup();
    expect(await screen.findByTestId("push-browser")).toHaveAttribute("data-state", "home-screen");
    const steps = screen.getByRole("list", { name: "Cách thêm hub vào màn hình chính" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "Bật push cho trình duyệt này" })).toBeNull();
  });

  it("says push is off on a hub without a VAPID key", async () => {
    api.config = OFF;
    browser();
    setup();
    expect(await screen.findByTestId("push-browser")).toHaveAttribute("data-state", "hub-off");
    expect(screen.queryByTestId("push-browser-on")).toBeNull();
  });

  it("says a browser without push cannot take it", async () => {
    browser({ push: false });
    setup();
    expect(await screen.findByTestId("push-browser")).toHaveAttribute("data-state", "unsupported");
  });
});

describe("devices", () => {
  it("lists the devices, sends a test and says how it went, and removes one once confirmed", async () => {
    browser({ push: false });
    api.devices = [LAPTOP, { id: 5, kind: "apns", label: null, created_at: "2026-10-19T03:00:00Z", last_sent_at: null, this_session: false }];
    const user = setup();
    const rows = await screen.findAllByTestId("push-device");
    expect(rows.map((row) => within(row).getByTestId("push-device-name").textContent)).toEqual(["Firefox on Linux", "App trên iPhone hay iPad"]);
    expect(within(rows[1]).getByTestId("push-device-facts")).toHaveTextContent("chưa nhận push nào");
    expect(screen.getByTestId("push-devices-count")).toHaveTextContent("2 thiết bị");

    await user.click(within(rows[0]).getByRole("button", { name: "Gửi push thử tới Firefox on Linux" }));
    expect(await screen.findByText("Đã gửi push thử tới Firefox on Linux")).toBeInTheDocument();

    api.test = () => Response.json({ sent: false, removed: true, error: null });
    await user.click(within(rows[0]).getByTestId("push-device-test"));
    expect(await screen.findByText("Firefox on Linux không còn nhận push")).toBeInTheDocument();

    api.test = () => Response.json({ error: "unavailable", message: "APNs is off on this hub" }, { status: 503 });
    await user.click(within(rows[1]).getByTestId("push-device-test"));
    expect(await screen.findByText("Không gửi được push thử tới App trên iPhone hay iPad")).toBeInTheDocument();

    await user.click(within(rows[1]).getByRole("button", { name: "Gỡ App trên iPhone hay iPad" }));
    const dialog = await screen.findByTestId("push-device-remove-dialog");
    expect(dialog).toHaveTextContent("Gỡ App trên iPhone hay iPad?");
    await user.click(within(dialog).getByTestId("push-device-remove-dialog-confirm"));
    await waitFor(() => expect(screen.queryByTestId("push-device-remove-dialog")).toBeNull());
    expect(writes().filter((request) => request.method === "DELETE")).toEqual([{ method: "DELETE", path: "/v1/me/push/devices/5", body: null }]);
    expect(await screen.findByText("Đã gỡ App trên iPhone hay iPad")).toBeInTheDocument();
  });

  it("says where push is turned on when no device has it", async () => {
    browser({ push: false });
    setup();
    expect(await screen.findByTestId("push-devices-empty")).toHaveTextContent("Chưa có thiết bị nào nhận push");
  });
});

describe("tiers, quiet hours and the lock screen", () => {
  it("shows the kinds the hub pushes first, folds the rest, and sets a tier or puts it back to the default", async () => {
    browser({ push: false });
    api.put = (body) => {
      const tiers = body.tiers as { kind: string; tier: string | null }[];
      api.settings = {
        ...api.settings,
        tiers: api.settings.tiers.map((setting) =>
          setting.kind === tiers[0].kind ? { ...setting, tier: (tiers[0].tier ?? "interrupt") as "interrupt", chosen: tiers[0].tier !== null } : setting,
        ),
      };
      return Response.json(api.settings);
    };
    const user = setup();
    const first = await screen.findByTestId("tiers-first");
    expect(within(first).getAllByTestId("tier").map((row) => row.getAttribute("data-kind"))).toEqual(["decision", "run_failed", "release", "merge_paused", "proposal", "today"]);
    const rest = screen.getByTestId("tiers-rest");
    expect(rest).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "Thêm 3 loại" }));
    expect(rest).toBeVisible();

    const select = within(first).getAllByTestId("tier-select")[1];
    expect(select).toHaveValue("default");
    expect(within(select).getByRole("option", { name: "Mặc định: Push ngay" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "off" } });
    await waitFor(() => expect(writes()).toEqual([{ method: "PUT", path: "/v1/me/notification-settings", body: { tiers: [{ kind: "run_failed", tier: "off" }] } }]));
    expect(await screen.findByText("Run thất bại: Chỉ Inbox")).toBeInTheDocument();
    await waitFor(() => expect(select).toHaveValue("off"));
    fireEvent.change(select, { target: { value: "default" } });
    await waitFor(() => expect(writes()[1]).toEqual({ method: "PUT", path: "/v1/me/notification-settings", body: { tiers: [{ kind: "run_failed", tier: null }] } }));
  });

  it("starts the time zone from this browser, saves quiet hours with it, and refuses what the hub would", async () => {
    browser({ push: false });
    api.put = (body) => {
      if (body.time_zone === "Mars/Base") return Response.json({ error: "validation", message: "'Mars/Base' is not a time zone the hub knows" }, { status: 422 });
      api.settings = { ...api.settings, quiet_hours: body.quiet_hours as never, time_zone: body.time_zone as string, time_zone_chosen: true, digest_at: body.digest_at as string };
      return Response.json(api.settings);
    };
    const user = setup();
    const form = await screen.findByTestId("schedule-form");
    const zone = within(form).getByTestId("schedule-time-zone");
    expect(zone).toHaveValue(OWN_ZONE);
    expect(within(form).getByTestId("schedule-time-zone-hint")).toHaveTextContent(`Hub dùng ${HUB_ZONE} cho tới khi bạn lưu`);

    await user.click(within(form).getByRole("switch", { name: "Giờ yên lặng" }));
    const end = within(form).getByTestId("schedule-end");
    fireEvent.change(end, { target: { value: "22:00" } });
    await user.click(within(form).getByTestId("schedule-save"));
    expect(within(form).getByTestId("schedule-problem")).toHaveTextContent("Giờ kết thúc phải khác giờ bắt đầu.");
    expect(end).toHaveFocus();
    expect(writes()).toEqual([]);

    fireEvent.change(end, { target: { value: "06:30" } });
    expect(within(form).getByTestId("schedule-overnight")).toHaveTextContent("Từ 22:00 tối nay đến 06:30 sáng mai.");
    await user.clear(zone);
    await user.type(zone, "Mars/Base");
    await user.click(within(form).getByTestId("schedule-save"));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Hub không nhận các cài đặt này.");

    await user.clear(zone);
    await user.type(zone, "Asia/Tokyo");
    await user.click(within(form).getByTestId("schedule-save"));
    expect(await screen.findByText("Đã lưu giờ yên lặng và tóm tắt")).toBeInTheDocument();
    expect(writes().at(-1)).toEqual({
      method: "PUT",
      path: "/v1/me/notification-settings",
      body: { quiet_hours: { start: "22:00", end: "06:30" }, time_zone: "Asia/Tokyo", digest_at: "07:30" },
    });
    await waitFor(() => expect(within(form).getByTestId("schedule-save")).toBeDisabled());
  });

  it("hides details on the lock screen as soon as it is flipped", async () => {
    browser({ push: false });
    const user = setup();
    const row = await screen.findByTestId("hide-details");
    await user.click(within(row).getByRole("switch", { name: "Ẩn chi tiết trên màn hình khoá" }));
    expect(await screen.findByText("Đã ẩn chi tiết trên màn hình khoá")).toBeInTheDocument();
    expect(writes()).toEqual([{ method: "PUT", path: "/v1/me/notification-settings", body: { hide_details: true } }]);
    await waitFor(() => expect(within(row).getByRole("switch")).toBeChecked());
  });
});
