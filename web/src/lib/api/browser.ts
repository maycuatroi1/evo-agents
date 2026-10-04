import { createApiClient, type ApiClient } from "./client";

let client: ApiClient | null = null;

/** The API from the browser: same origin (/v1 is routed to the API), so the session cookie goes with it. */
export function browserApi(): ApiClient {
  if (typeof window === "undefined") throw new Error("browserApi() runs in the browser only; use serverApi()");
  client ??= createApiClient({ baseUrl: window.location.origin, headers: { accept: "application/json" } });
  return client;
}
