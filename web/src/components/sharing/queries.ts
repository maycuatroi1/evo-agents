import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";

import type { CredentialShare, ShareWrite } from "./model";

/**
 * What the share dialog and the list of shares read and write: the org chart, for the units and the members a share
 * may name (GET /v1/org, which every member reads), a new share of one of the visitor's secrets or accounts, and the
 * revocation of one. The hub decides who may share what (`evo_agents.hub.server.credential_shares`) and says why not.
 */

/** The same key as the Org page's chart, so the two read it once. */
export const shareKeys = { chart: ["org", "chart"] as const };

export const shareChartQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: shareKeys.chart,
    queryFn: ({ signal }) => call(api().GET("/v1/org", { signal })),
    staleTime: 60_000,
  });

// Writes, each with the session's X-Evo-CSRF header.

/** Share the visitor's secret or account `name`. */
export async function createShare(api: ApiClient, name: string, body: ShareWrite): Promise<CredentialShare> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/secrets/{name}/shares", { params: { path: { name } }, body, headers }));
}

/** Revoke share `id`: the leases taken under it end, and no run takes it again. */
export async function revokeShare(api: ApiClient, id: number): Promise<CredentialShare> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/shares/{share_id}", { params: { path: { share_id: id } }, headers }));
}
