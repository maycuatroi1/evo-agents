import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the page /settings/autonomy reads and writes (plan autonomy-levels, step 7): the caller's own autonomy, their
 * preference and the ceiling over it, the stop that holds them and each class's figures of the last 30 days (GET
 * /v1/autonomy/settings), and the next revision of their preference (PUT /v1/autonomy/prefs). The hub decides: a
 * preference above the ceiling is stored, and the ceiling still bounds it.
 */
type Schemas = components["schemas"];
export type AutonomySettings = Schemas["AutonomySettings"];
export type SettingsClass = Schemas["SettingsClass"];
export type ClassFigures = Schemas["ClassFigures"];
export type HeldStop = Schemas["HeldStop"];
export type PrefSet = Schemas["PrefSet"];
export type Level = NonNullable<PrefSet["level"]>;

export const AUTONOMY_HREF = "/settings/autonomy" as Route;

export const autonomyKeys = {
  settings: ["autonomy", "settings"] as const,
};

export const settingsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: autonomyKeys.settings,
    queryFn: ({ signal }) => call(api().GET("/v1/autonomy/settings", { signal })),
  });

export async function writePref(api: ApiClient, body: PrefSet) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/autonomy/prefs", { body, headers }));
}
