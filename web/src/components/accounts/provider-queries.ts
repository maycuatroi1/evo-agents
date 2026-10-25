import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { accountKeys } from "./queries";

/**
 * What the provider cards, the catalog and the provider form read and write (docs/credentials.md, section "Provider
 * accounts"): the catalog of model providers (GET /v1/model-catalog), the visitor's provider accounts, each an API key
 * of one provider the hub seals and never shows again (/v1/provider-accounts), Test connection and Fetch models (its
 * probe), and whether the hub's model gateway, which runs their models, is set up (GET /v1/health).
 */
type Schemas = components["schemas"];
export type CatalogProvider = Schemas["CatalogProvider"];
export type ModelCatalog = Schemas["ModelCatalog"];
export type ProviderAccount = Schemas["ProviderAccount"];
export type ProviderWrite = Schemas["ProviderAccountWrite"];
export type ProviderChange = Schemas["ProviderAccountChange"];
export type ProviderProbe = Schemas["ProviderProbe"];
export type ProbeWrite = Schemas["ProbeWrite"];
export type ApiFormat = ProviderAccount["api_format"];
export type GatewayState = Schemas["Health"]["gateway"] | "unknown";

/** `provider_accounts.API_FORMATS`, in the order the form offers them. */
export const API_FORMATS = ["openai", "anthropic", "openai-responses", "gemini"] as const satisfies readonly ApiFormat[];
/** `provider_accounts.MAX_MODELS` and `MODEL_NAME`: what one account may list. */
export const MAX_MODELS = 500;
export const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$/;

export const providerKeys = {
  list: [...accountKeys.all, "providers"] as const,
  gateway: [...accountKeys.all, "gateway"] as const,
  catalog: ["model-catalog"] as const,
};

/** The visitor's provider accounts, by name, with their models and bindings; never a key. */
export const providerAccountsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: providerKeys.list,
    queryFn: ({ signal }) => call(api().GET("/v1/provider-accounts", { signal })),
    refetchInterval: 30_000,
  });

/** The catalog: the same for everyone and changed only by a new hub, so it is read once per page load. */
export const catalogQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: providerKeys.catalog,
    queryFn: ({ signal }) => call(api().GET("/v1/model-catalog", { signal })),
    staleTime: Infinity,
  });

/** Whether the model gateway is set up (ok), unreachable (error) or off; unknown when the check failed. */
export const gatewayStateQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: providerKeys.gateway,
    queryFn: async ({ signal }): Promise<GatewayState> => {
      try {
        const { data, error } = await api().GET("/v1/health", { signal });
        return (data ?? error)?.gateway ?? "unknown";
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
        return "unknown";
      }
    },
    staleTime: 60_000,
  });

// Writes, each with the session's X-Evo-CSRF header.

export async function putProviderAccount(api: ApiClient, name: string, body: ProviderWrite) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/provider-accounts/{name}", { params: { path: { name } }, body, headers }));
}

/** Change what `body` gives and keep the rest; a key only when the person gave a new one. */
export async function changeProviderAccount(api: ApiClient, name: string, body: ProviderChange) {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/provider-accounts/{name}", { params: { path: { name } }, body, headers }));
}

export async function deleteProviderAccount(api: ApiClient, name: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/provider-accounts/{name}", { params: { path: { name } }, headers }));
}

/** Test connection or Fetch models of an endpoint not saved yet, or of the saved account `name` with its own key. */
export async function probeProvider(api: ApiClient, target: { body: ProbeWrite } | { name: string; model: string | null }): Promise<ProviderProbe> {
  const headers = await csrfHeaders(api);
  if ("body" in target) return call(api.POST("/v1/provider-accounts/probe", { body: target.body, headers }));
  const params = { path: { name: target.name } };
  return call(api.POST("/v1/provider-accounts/{name}/probe", { params, body: { model: target.model }, headers }));
}
