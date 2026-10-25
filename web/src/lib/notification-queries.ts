import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of a member's notifications the web shares (docs/notifications.md): their settings (GET and PUT
 * /v1/me/notification-settings), which the Inbox's Notices dialog and the settings page both use, the hub's push
 * configuration and the member's push devices (/v1/push/config, /v1/me/push/devices), and Today (GET /v1/me/today,
 * POST /v1/me/today/seen), which Home opens with.
 */
type Schemas = components["schemas"];
export type NotificationSettings = Schemas["NotificationSettings"];
export type NotificationSettingsIn = Schemas["NotificationSettingsIn"];
export type TierSetting = Schemas["TierSetting"];
export type TierKind = TierSetting["kind"];
export type Tier = TierSetting["tier"];
export type PushConfig = Schemas["PushConfig"];
export type PushDevice = Schemas["PushDevice"];
export type PushTestResult = Schemas["PushTestResult"];
export type Today = Schemas["Today"];
export type TodayItem = Schemas["TodayItem"];

export const notificationKeys = {
  settings: ["me", "notification-settings"] as const,
  pushConfig: ["push", "config"] as const,
  devices: ["me", "push", "devices"] as const,
  today: ["me", "today"] as const,
};

export const notificationSettingsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: notificationKeys.settings,
    queryFn: ({ signal }) => call(api().GET("/v1/me/notification-settings", { signal })),
  });

export async function putNotificationSettings(api: ApiClient, body: NotificationSettingsIn): Promise<NotificationSettings> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/me/notification-settings", { body, headers }));
}

/** The hub's public VAPID key and its senders: the same for every visitor, and asked again only on a reload. */
export const pushConfigQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: notificationKeys.pushConfig,
    queryFn: ({ signal }) => call(api().GET("/v1/push/config", { signal })),
    staleTime: Infinity,
  });

export const pushDevicesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: notificationKeys.devices,
    queryFn: ({ signal }) => call(api().GET("/v1/me/push/devices", { signal })),
  });

/** A browser's Web Push subscription, as `PushSubscription.toJSON()` gives it. */
export type WebPushSubscriptionJson = { endpoint: string; keys: { p256dh: string; auth: string } };

export async function addWebPushDevice(api: ApiClient, subscription: WebPushSubscriptionJson, label: string | null): Promise<PushDevice> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/me/push/devices", { body: { kind: "webpush", label, subscription }, headers }));
}

export async function removePushDevice(api: ApiClient, id: number): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/me/push/devices/{device_id}", { params: { path: { device_id: id } }, headers }));
}

export async function testPushDevice(api: ApiClient, id: number): Promise<PushTestResult> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/me/push/devices/{device_id}/test", { params: { path: { device_id: id } }, headers }));
}

/**
 * Today, read once for each visit of Home: the server reads it with the page, and the browser keeps that answer while
 * the page is open, since Home moves the seen mark once its items have shown and a later read would take them away
 * from under the visitor's eyes. The next visit reads it again.
 */
export const todayQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: notificationKeys.today,
    queryFn: ({ signal }) => call(api().GET("/v1/me/today", { signal })),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

/** Move the seen mark to `seenAt` (Today's `seen_until`); the hub keeps the later of it and the mark it holds. */
export async function markTodaySeen(api: ApiClient, seenAt: string): Promise<string> {
  const headers = await csrfHeaders(api);
  const moved = await call(api.POST("/v1/me/today/seen", { body: { seen_at: seenAt }, headers }));
  return moved.seen_at;
}
