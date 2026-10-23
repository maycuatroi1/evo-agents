import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { pluginPageHref } from "@/lib/plugin-queries";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of the core plugin marketing (evo_agents.core_plugins.marketing.server, /v1/x/marketing/{p}),
 * as TanStack Query options the server (prefetch) and the browser share. A reader reads the status, the channels and
 * the week; the posts are a writer's; the kill switch is any writer's and only an admin clears it or closes a breaker.
 */
type Schemas = components["schemas"];
export type MarketingStatus = Schemas["MarketingStatus"];
export type MarketingChannel = Schemas["MarketingChannel"];
export type MarketingPost = Schemas["MarketingPost"];
export type MarketingBudget = Schemas["MarketingBudget"];
export type MarketingWeekly = Schemas["MarketingWeekly"];

export const MARKETING_PLUGIN = "marketing";
/** The plugin's pages in the project's menu. */
export const MARKETING_OVERVIEW = "overview";
export const MARKETING_POSTS = "posts";
export const MARKETING_WEEKLY = "weekly";
export const MARKETING_PAGES = [MARKETING_OVERVIEW, MARKETING_POSTS, MARKETING_WEEKLY] as const;
export type MarketingPage = (typeof MARKETING_PAGES)[number];

/** The reason the kill switch keeps when it is pressed on this page. */
export const KILL_REASON = "stopped from the web";

export const marketingKeys = {
  all: (project: string) => ["projects", project, "plugins", "x", MARKETING_PLUGIN] as const,
  status: (project: string) => [...marketingKeys.all(project), "status"] as const,
  channels: (project: string) => [...marketingKeys.all(project), "channels"] as const,
  posts: (project: string) => [...marketingKeys.all(project), "posts"] as const,
  weekly: (project: string) => [...marketingKeys.all(project), "weekly"] as const,
};

export const marketingStatusQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: marketingKeys.status(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/marketing/{project}/status", { params: { path: { project } }, signal })),
  });

export const marketingChannelsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: marketingKeys.channels(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/marketing/{project}/channels", { params: { path: { project } }, signal })),
  });

export const marketingPostsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: marketingKeys.posts(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/marketing/{project}/posts", { params: { path: { project } }, signal })),
  });

export const marketingWeeklyQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: marketingKeys.weekly(project),
    queryFn: ({ signal }) => call(api().GET("/v1/x/marketing/{project}/weekly", { params: { path: { project } }, signal })),
  });

export async function killPosting(api: ApiClient, project: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/marketing/{project}/kill", { params: { path: { project } }, body: { reason: KILL_REASON }, headers }));
}

export async function resumePosting(api: ApiClient, project: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/x/marketing/{project}/resume", { params: { path: { project } }, headers }));
}

export async function closeBreaker(api: ApiClient, project: string, channelId: number) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/x/marketing/{project}/channels/{channel_id}/resume", { params: { path: { project, channel_id: channelId } }, headers }),
  );
}

export function marketingHref(project: string, page: MarketingPage = MARKETING_OVERVIEW): Route {
  return pluginPageHref(project, MARKETING_PLUGIN, page);
}

/** What the month has used of its cap: spent plus what posts in flight hold, 0 to 100 (more once over). */
export function budgetUsed(budget: MarketingBudget): number {
  if (budget.budget_usd <= 0) return 100;
  return ((budget.spent_usd + budget.reserved_usd) / budget.budget_usd) * 100;
}

/** The month's money is gone: nothing more is posted that costs until the next month or a higher cap. */
export function budgetSpent(budget: MarketingBudget): boolean {
  return budget.spent_usd + budget.reserved_usd >= budget.budget_usd;
}
