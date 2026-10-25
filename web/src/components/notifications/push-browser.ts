/**
 * Web Push in this browser (docs/notifications.md, Push): whether it can take push, its subscription through the
 * service worker at /sw.js, and the device of the hub it is. Only the settings page subscribes, and only from a click,
 * as Safari requires (https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/); on an iPhone or an iPad
 * that is only once the hub was added to the Home Screen and opened from there.
 */

export const SERVICE_WORKER = "/sw.js";

/**
 * supported: the browser can subscribe; homeScreen: an iPhone or iPad whose Safari takes push only in a web app
 * added to the Home Screen; unsupported: no push at all (an old browser, or an insecure origin).
 */
export type PushSupport = "supported" | "homeScreen" | "unsupported";

export type BrowserFacts = {
  userAgent: string;
  maxTouchPoints: number;
  standalone: boolean;
  serviceWorker: boolean;
  pushManager: boolean;
  notification: boolean;
};

/** An iPhone, an iPod or an iPad, including the iPad that names itself a Mac (Safari's desktop mode). */
export function isAppleMobile(userAgent: string, maxTouchPoints: number): boolean {
  return /iPhone|iPad|iPod/.test(userAgent) || (/Macintosh/.test(userAgent) && maxTouchPoints > 1);
}

export function pushSupport(facts: BrowserFacts): PushSupport {
  if (facts.serviceWorker && facts.pushManager && facts.notification) return "supported";
  if (isAppleMobile(facts.userAgent, facts.maxTouchPoints) && !facts.standalone) return "homeScreen";
  return "unsupported";
}

/** What this browser is, read once it runs: on the server there is no browser, and the caller renders nothing yet. */
export function browserFacts(): BrowserFacts {
  const standalone =
    (typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches) ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return {
    userAgent: navigator.userAgent,
    maxTouchPoints: navigator.maxTouchPoints ?? 0,
    standalone,
    serviceWorker: "serviceWorker" in navigator,
    pushManager: "PushManager" in window,
    notification: "Notification" in window,
  };
}

/** A name for this browser's device that its owner recognises in the list: "Chrome on macOS", "Safari on iPhone". */
export function deviceLabel(userAgent: string): string {
  const browser = /Edg\//.test(userAgent)
    ? "Edge"
    : /OPR\//.test(userAgent)
      ? "Opera"
      : /Firefox\/|FxiOS\//.test(userAgent)
        ? "Firefox"
        : /Chrome\/|CriOS\//.test(userAgent)
          ? "Chrome"
          : /Safari\//.test(userAgent)
            ? "Safari"
            : "Browser";
  const system = /iPhone/.test(userAgent)
    ? "iPhone"
    : /iPad/.test(userAgent)
      ? "iPad"
      : /Android/.test(userAgent)
        ? "Android"
        : /Windows/.test(userAgent)
          ? "Windows"
          : /Mac OS X|Macintosh/.test(userAgent)
            ? "macOS"
            : /CrOS/.test(userAgent)
              ? "ChromeOS"
              : /Linux/.test(userAgent)
                ? "Linux"
                : null;
  return system ? `${browser} on ${system}` : browser;
}

/** A VAPID public key in base64url as the bytes `pushManager.subscribe` takes. */
export function keyBytes(key: string): Uint8Array<ArrayBuffer> {
  const base64 = key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (key.length % 4)) % 4);
  const binary = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/** Whether a subscription was made with `key`; one made with another key (a hub that changed its key) is replaced. */
export function sameKey(held: ArrayBuffer | null | undefined, key: Uint8Array): boolean {
  if (!held) return true; // the browser does not say: keep the subscription
  const bytes = new Uint8Array(held);
  return bytes.length === key.length && bytes.every((value, index) => value === key[index]);
}

// The device this browser is, as the hub numbers it: kept in this browser, since the list does not name endpoints.

const MARK = "evo-push-device";

export type DeviceMark = { id: number; endpoint: string };

export function readMark(): DeviceMark | null {
  try {
    const raw = window.localStorage.getItem(MARK);
    const found = raw ? (JSON.parse(raw) as Partial<DeviceMark>) : null;
    return found && typeof found.id === "number" && typeof found.endpoint === "string" ? { id: found.id, endpoint: found.endpoint } : null;
  } catch {
    return null;
  }
}

export function writeMark(mark: DeviceMark | null): void {
  try {
    if (mark) window.localStorage.setItem(MARK, JSON.stringify(mark));
    else window.localStorage.removeItem(MARK);
  } catch {
    // Storage refused (a private window): the page still works, it only cannot tell this browser's row apart.
  }
}

/** This browser's subscription, if its service worker has one. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  const registration = await navigator.serviceWorker.getRegistration("/");
  return registration ? registration.pushManager.getSubscription() : null;
}

/**
 * Subscribe this browser with the hub's key: register the service worker, wait for it, and keep a subscription made
 * with the same key or make a new one. Permission must be granted already.
 */
export async function subscribe(vapidKey: string): Promise<PushSubscription> {
  await navigator.serviceWorker.register(SERVICE_WORKER, { scope: "/" });
  const registration = await navigator.serviceWorker.ready;
  const key = keyBytes(vapidKey);
  const held = await registration.pushManager.getSubscription();
  if (held && sameKey(held.options.applicationServerKey, key)) return held;
  if (held) await held.unsubscribe();
  return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
}

/** The subscription as the hub takes it (`PushSubscription.toJSON()`, without the expiry the hub does not keep). */
export function subscriptionJson(subscription: PushSubscription): { endpoint: string; keys: { p256dh: string; auth: string } } {
  const json = subscription.toJSON();
  return { endpoint: subscription.endpoint, keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" } };
}
