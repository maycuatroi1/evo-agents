import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pluginKeys } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of the market pages (docs/market.md): the plugins of every market the hub reads, for every member; one of
 * them with what its latest release asks for; and, for hub admins, the markets themselves and the publishers the hub
 * trusts (GET /v1/plugin-publishers, which the store's page reads too).
 */

export type MarketList = components["schemas"]["MarketList"];
export type MarketPlugin = components["schemas"]["MarketPlugin"];
export type MarketPluginDetail = components["schemas"]["MarketPluginDetail"];
export type MarketStatus = MarketPlugin["status"];
export type MarketSource = components["schemas"]["MarketSource"];
export type MarketSources = components["schemas"]["MarketSources"];
export type PluginPublisher = components["schemas"]["PluginPublisher"];

export const marketKeys = {
  all: ["market"] as const,
  list: ["market", "list"] as const,
  one: (source: number, plugin: string) => ["market", "one", source, plugin] as const,
  sources: ["market", "sources"] as const,
};

export const marketQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: marketKeys.list,
    queryFn: ({ signal }) => call(api().GET("/v1/market", { signal })),
  });

export const marketPluginQuery = (api: ApiSource, source: number, plugin: string) =>
  queryOptions({
    queryKey: marketKeys.one(source, plugin),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/market/{source}/{plugin}", { params: { path: { source, plugin } }, signal })),
  });

export const marketSourcesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: marketKeys.sources,
    queryFn: ({ signal }) => call(api().GET("/v1/market/sources", { signal })),
  });

export const trustedPublishersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: pluginKeys.publishers,
    queryFn: ({ signal }) => call(api().GET("/v1/plugin-publishers", { signal })),
  });

export function marketHref(): Route {
  return "/market";
}

export function marketPluginHref(source: number, plugin: string): Route {
  return `/market/${source}/${encodeURIComponent(plugin)}` as Route;
}

export function adminMarketHref(): Route {
  return "/admin/market";
}

/** The plugin in the hub's store, where a hub admin enables its config and removes it. */
export function storePluginHref(name: string): Route {
  return `/admin/plugins/${encodeURIComponent(name)}` as Route;
}
