// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { deviceLabel, isAppleMobile, keyBytes, pushSupport, readMark, sameKey, subscriptionJson, writeMark } from "./push-browser";

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const IPAD_AS_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15";
const CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";
const EDGE_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0";
const FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0";
const CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36";

const full = { serviceWorker: true, pushManager: true, notification: true, standalone: false, maxTouchPoints: 0 };

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("push in this browser", () => {
  it("can push where the browser has a service worker, PushManager and Notification", () => {
    expect(pushSupport({ ...full, userAgent: CHROME_MAC })).toBe("supported");
    expect(pushSupport({ ...full, userAgent: IPHONE, standalone: true })).toBe("supported");
  });

  it("sends an iPhone or an iPad outside a Home Screen app to the Home Screen, and says no to anything else without push", () => {
    const safari = { ...full, pushManager: false, notification: false };
    expect(pushSupport({ ...safari, userAgent: IPHONE })).toBe("homeScreen");
    expect(pushSupport({ ...safari, userAgent: IPAD_AS_MAC, maxTouchPoints: 5 })).toBe("homeScreen");
    expect(pushSupport({ ...safari, userAgent: IPAD_AS_MAC, maxTouchPoints: 0 })).toBe("unsupported");
    expect(pushSupport({ ...safari, userAgent: IPHONE, standalone: true })).toBe("unsupported");
    expect(pushSupport({ ...full, serviceWorker: false, userAgent: FIREFOX_LINUX })).toBe("unsupported");
    expect(isAppleMobile(IPHONE, 5)).toBe(true);
    expect(isAppleMobile(CHROME_MAC, 0)).toBe(false);
  });

  it("names the device by its browser and system", () => {
    expect(deviceLabel(IPHONE)).toBe("Safari on iPhone");
    expect(deviceLabel(CHROME_MAC)).toBe("Chrome on macOS");
    expect(deviceLabel(EDGE_WINDOWS)).toBe("Edge on Windows");
    expect(deviceLabel(FIREFOX_LINUX)).toBe("Firefox on Linux");
    expect(deviceLabel(CHROME_ANDROID)).toBe("Chrome on Android");
    expect(deviceLabel("curl/8.0")).toBe("Browser");
  });

  it("reads the hub's key as bytes, and keeps a subscription only when it was made with that key", () => {
    const key = keyBytes("BAEC_-8");
    expect([...key]).toEqual([4, 1, 2, 255, 239]);
    expect(sameKey(new Uint8Array([4, 1, 2, 255, 239]).buffer, key)).toBe(true);
    expect(sameKey(new Uint8Array([4, 1, 2, 255, 238]).buffer, key)).toBe(false);
    expect(sameKey(new Uint8Array([4, 1]).buffer, key)).toBe(false);
    expect(sameKey(null, key)).toBe(true);
  });

  it("gives the hub the subscription's endpoint and keys, not its expiry", () => {
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/abc",
      toJSON: () => ({ endpoint: "https://fcm.googleapis.com/fcm/send/abc", expirationTime: 1, keys: { p256dh: "BPk", auth: "c2Vj" } }),
    } as unknown as PushSubscription;
    expect(subscriptionJson(subscription)).toEqual({ endpoint: "https://fcm.googleapis.com/fcm/send/abc", keys: { p256dh: "BPk", auth: "c2Vj" } });
  });

  it("keeps this browser's device in its storage, and works on when storage refuses", () => {
    expect(readMark()).toBeNull();
    writeMark({ id: 7, endpoint: "https://web.push.apple.com/x" });
    expect(readMark()).toEqual({ id: 7, endpoint: "https://web.push.apple.com/x" });
    window.localStorage.setItem("evo-push-device", "{not json");
    expect(readMark()).toBeNull();
    writeMark(null);
    expect(window.localStorage.getItem("evo-push-device")).toBeNull();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    expect(() => writeMark({ id: 8, endpoint: "e" })).not.toThrow();
  });
});
