import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The member's Telegram chat (docs/notifications.md, Telegram): whether the hub has a bot, whether a chat of theirs is
 * linked to it, the one-time link that links one, and unlinking. The hub decides on every write; the dialog only shows
 * where things stand.
 */
type Schemas = components["schemas"];
export type TelegramStatus = Schemas["TelegramStatus"];
export type TelegramLink = Schemas["TelegramLink"];

/** While a link waits for Telegram, the dialog asks the hub this often whether the chat is linked yet. */
export const TELEGRAM_WAIT_MS = 3_000;

export const telegramKeys = { status: ["me", "telegram"] as const };

/** Where the member's link stands; asked every few seconds while `waiting` (a link is open in Telegram). */
export const telegramQuery = (api: ApiSource, waiting = false) =>
  queryOptions({
    queryKey: telegramKeys.status,
    queryFn: ({ signal }) => call(api().GET("/v1/me/telegram", { signal })),
    refetchInterval: waiting ? TELEGRAM_WAIT_MS : false,
  });

/** A one-time link that links the Telegram chat it is opened in, for 10 minutes. */
export async function makeTelegramLink(api: ApiClient): Promise<TelegramLink> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/me/telegram/link", { headers }));
}

/** Unlink the member's chat: the channel and the deliveries waiting for it go. */
export async function unlinkTelegram(api: ApiClient): Promise<TelegramStatus> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/me/telegram", { headers }));
}

/**
 * What the dialog shows: the hub has no bot (`unconfigured`); no chat is linked (`unlinked`); a link waits to be opened
 * in Telegram (`pending`) or expired unused (`expired`); a chat is linked and gets messages (`linked`); or the hub
 * turned it off (`off`): Telegram refused it, as when the bot was blocked, or the web session that linked it ended.
 */
export type TelegramView = "unconfigured" | "unlinked" | "pending" | "expired" | "linked" | "off";

export function telegramView(status: Pick<TelegramStatus, "configured" | "linked" | "enabled">, link: Pick<TelegramLink, "expires_at"> | null, now: number): TelegramView {
  if (!status.configured) return "unconfigured";
  if (status.linked && status.enabled) return "linked";
  if (link) return Date.parse(link.expires_at) > now ? "pending" : "expired";
  return status.linked ? "off" : "unlinked";
}

