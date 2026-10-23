import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { invitePath } from "@/lib/invite-path";
import type { ApiSource } from "@/lib/queries";

import { adminKeys, type Role } from "./data";

/**
 * Invite links, as the admin area makes, lists and revokes them (`hub.server.invites`). A link is shown in full only
 * in the answer that makes it; the list never holds a token.
 */

export type InviteRow = components["schemas"]["InviteRow"];
export type InviteCreated = components["schemas"]["InviteCreated"];
export type InviteKind = InviteRow["kind"];
export const INVITE_KINDS: readonly InviteKind[] = ["join", "register"];

/** `invites.DEFAULT_DAYS` and `MAX_DAYS`: how long a link stays open. */
export const INVITE_DAYS = { min: 1, max: 30, initial: 7 } as const;

/** Under `adminKeys.all`, so every admin write reloads the list as it reloads the others. */
export const invitesKey = [...adminKeys.all, "invites"] as const;

export const adminInvitesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: invitesKey,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/invites", { signal })),
  });

export type InviteInput = {
  kind: InviteKind;
  project: string;
  role: Role;
  maxLevel: string;
  forLogin: string;
  days: number;
};

/** POST /v1/admin/invites: a join link names its project, role and level; a register link names none. */
export async function createInvite(api: ApiClient, input: InviteInput): Promise<InviteCreated> {
  const headers = await csrfHeaders(api);
  const forLogin = input.forLogin.trim() || null;
  const body =
    input.kind === "join"
      ? { kind: input.kind, project: input.project, role: input.role, max_level: input.maxLevel, for_login: forLogin, days: input.days }
      : { kind: input.kind, for_login: forLogin, days: input.days };
  return call(api.POST("/v1/admin/invites", { body, headers }));
}

export async function revokeInvite(api: ApiClient, id: number): Promise<InviteRow> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/admin/invites/{invite_id}/revoke", { params: { path: { invite_id: id } }, headers }));
}

/** The link to hand out: the hub's own (EVO_HUB_PUBLIC_URL), or this web's page of it when the hub has no public URL. */
export function inviteLink(created: Pick<InviteCreated, "url" | "token">, origin: string): string {
  return created.url ?? `${origin}${invitePath(created.token)}`;
}

/** What the API revokes: an open link, or the right an accepted register link gave while no registration used it. */
export function revocable(row: Pick<InviteRow, "status" | "kind" | "used_at">): boolean {
  return row.status === "open" || (row.kind === "register" && row.status === "accepted" && row.used_at === null);
}

/** Whether `value` is a number of days the API takes. */
export function validDays(value: string): boolean {
  const days = Number(value);
  return /^\d+$/.test(value.trim()) && days >= INVITE_DAYS.min && days <= INVITE_DAYS.max;
}
