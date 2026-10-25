import { generateKeyPairSync, randomBytes } from "node:crypto";

import type { BrowserContext } from "@playwright/test";

import { call } from "../../src/lib/api/client";

import type { Account } from "./hub";
import { bearerClient, machineToken } from "./hub";

/**
 * Web Push for the specs (plan hub-mobile-phase-0): Playwright's browsers have no push service, so a spec fakes the
 * browser's PushManager, and the hub keeps the subscription it is handed as a device like any other.
 */

/** A subscription as a browser would make one: an endpoint of a push service the hub accepts, and real keys. */
export function fakeSubscription(name: string) {
  const { publicKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const jwk = publicKey.export({ format: "jwk" });
  const point = Buffer.concat([Buffer.from([4]), Buffer.from(jwk.x!, "base64url"), Buffer.from(jwk.y!, "base64url")]);
  return {
    endpoint: `https://fcm.googleapis.com/fcm/send/e2e-${name}-${randomBytes(6).toString("hex")}`,
    keys: { p256dh: point.toString("base64url"), auth: randomBytes(16).toString("base64url") },
  };
}

export type Subscription = ReturnType<typeof fakeSubscription>;

/**
 * The browser's push as the page sees it, set before the page loads: the site's notification permission, `default`
 * until the person answers the prompt (with `answer`), and a PushManager whose subscription lives in this tab's
 * sessionStorage, so it survives a reload as a real one would. Headless Chromium has no notifications of its own (its
 * permission reads denied whatever the context grants), so the permission is faked too; `window.e2ePermissionAsked`
 * counts the prompts.
 */
export async function fakePush(
  context: BrowserContext,
  subscription: Subscription,
  { permission = "default", answer = "granted" }: { permission?: NotificationPermission; answer?: NotificationPermission } = {},
) {
  await context.addInitScript(
    ({ subscription, permission, answer }) => {
      const KEY = "e2e-push-subscription";
      const PERMISSION = "e2e-push-permission";
      const make = () => ({
        endpoint: subscription.endpoint,
        expirationTime: null,
        options: { applicationServerKey: null, userVisibleOnly: true },
        toJSON: () => ({ endpoint: subscription.endpoint, expirationTime: null, keys: subscription.keys }),
        unsubscribe: async () => {
          sessionStorage.removeItem(KEY);
          return true;
        },
      });
      PushManager.prototype.subscribe = async function () {
        sessionStorage.setItem(KEY, "1");
        return make() as unknown as PushSubscription;
      };
      PushManager.prototype.getSubscription = async function () {
        return (sessionStorage.getItem(KEY) ? make() : null) as unknown as PushSubscription;
      };
      const current = () => (sessionStorage.getItem(PERMISSION) as NotificationPermission | null) ?? permission;
      const counted = window as unknown as { e2ePermissionAsked: number };
      counted.e2ePermissionAsked = 0;
      Object.defineProperty(Notification, "permission", { configurable: true, get: current });
      Notification.requestPermission = async () => {
        counted.e2ePermissionAsked += 1;
        if (current() === "default") sessionStorage.setItem(PERMISSION, answer);
        return current();
      };
    },
    { subscription, permission, answer },
  );
}

/** A device of `account`'s added through the API, as another browser of theirs would add it. */
export async function addPushDeviceByApi(account: Account, label: string) {
  const api = bearerClient(await machineToken(account));
  return call(api.POST("/v1/me/push/devices", { body: { kind: "webpush", label, subscription: fakeSubscription(label.toLowerCase().replace(/[^a-z0-9]+/g, "-")) } }));
}
