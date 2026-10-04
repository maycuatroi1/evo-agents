import "server-only";

import { cookies } from "next/headers";

import { SESSION_COOKIE } from "@/lib/config";

import { createApiClient, type ApiClient } from "./client";

/**
 * The API as a server component sees it: EVO_HUB_API_INTERNAL_URL (the API container on the internal network)
 * with the visitor's session cookie forwarded. Nothing else of the request is passed on, and nothing is kept.
 */
export function apiInternalUrl(): string {
  const url = process.env.EVO_HUB_API_INTERNAL_URL?.trim();
  if (!url) throw new Error("EVO_HUB_API_INTERNAL_URL is not set: the web cannot reach the hub API");
  return url.replace(/\/+$/, "");
}

export async function serverApi(): Promise<ApiClient> {
  const session = (await cookies()).get(SESSION_COOKIE)?.value;
  return createApiClient({
    baseUrl: apiInternalUrl(),
    headers: session ? { cookie: `${SESSION_COOKIE}=${session}` } : {},
    fetch: (request) => fetch(request, { cache: "no-store" }),
  });
}
