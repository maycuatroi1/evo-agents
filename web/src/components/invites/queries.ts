import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { API_LOGIN_PATH } from "@/lib/config";
import { invitePath } from "@/lib/invite-path";
import type { ApiSource } from "@/lib/queries";

/**
 * What the page of an invite link reads and writes. The preview is public (the token is the credential): it names the
 * inviter and, to someone the link is for, the project and role; accepting needs the session of the person it invites.
 */

export type InvitePreview = components["schemas"]["InvitePreview"];
export type AcceptedInvite = components["schemas"]["InviteRow"];

export const inviteKeys = {
  all: ["invite"] as const,
  preview: (token: string) => ["invite", "preview", token] as const,
};

/** POST /v1/invites/preview: a POST so the token travels in the body, never in a URL the API logs. */
export const invitePreviewQuery = (api: ApiSource, token: string) =>
  queryOptions({
    queryKey: inviteKeys.preview(token),
    queryFn: ({ signal }) => call(api().POST("/v1/invites/preview", { body: { token }, signal })),
  });

export async function acceptInvite(api: ApiClient, token: string): Promise<AcceptedInvite> {
  return call(api.POST("/v1/invites/accept", { body: { token }, headers: await csrfHeaders(api) }));
}

/** Ends the web session, for a visitor signed in with a login the link is not for. */
export async function signOut(api: ApiClient): Promise<void> {
  await call(api.POST("/v1/auth/web/logout", { headers: await csrfHeaders(api) }));
}

/** GitHub's sign-in, coming back to the page of the link `token`. */
export function signInHref(token: string): string {
  return `${API_LOGIN_PATH}?${new URLSearchParams({ next: invitePath(token) }).toString()}`;
}
