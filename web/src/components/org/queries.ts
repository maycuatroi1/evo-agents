import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Org page reads and writes (plan org-chart, docs/org-chart.md): the whole chart of GET /v1/org, each member's
 * runs at work over the projects the viewer holds a grant on (GET /v1/org/runs), and for a hub admin the people of the
 * hub with their grants (GET /v1/admin/users), for the Unplaced list. Every write is one route under /v1/org; the hub
 * decides who may make it (`evo_agents.hub.org`) and answers 403 naming why.
 */
type Schemas = components["schemas"];
export type OrgChart = Schemas["OrgChartView"];
export type OrgUnit = Schemas["OrgUnitView"];
export type OrgSeat = Schemas["OrgSeatView"];
export type OrgViewer = Schemas["OrgViewer"];
export type OrgRuns = Schemas["OrgRuns"];
export type OrgUser = Schemas["UserRow"];
export type Level = OrgUnit["effective"];
export type SeatRole = OrgSeat["role"];
export type UnitCreate = Schemas["UnitCreate"];
export type UnitEdit = Schemas["UnitEdit"];
export type SeatAdd = Schemas["SeatAdd"];
export type SeatEdit = Schemas["SeatEdit"];
export type CeilingSet = Schemas["CeilingSet"];
export type OrgAutonomy = Schemas["OrgAutonomy"];
export type HeldStop = Schemas["HeldStop"];
export type StopSet = Schemas["StopSet"];
export type StopPreview = Schemas["StopPreview"];

/** `org.LEVELS`, the lowest first. */
export const LEVELS = ["A0", "A1", "A2", "A3", "A4", "A5"] as const satisfies readonly Level[];
export const ROLES = ["lead", "member", "intern", "agent"] as const satisfies readonly SeatRole[];
/** `org.SLUG_PATTERN`. */
export const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
/** `org_models.LOGIN_PATTERN` and `HANDLE_PATTERN`. */
export const LOGIN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
export const HANDLE = /^[a-z0-9][a-z0-9-]{0,99}$/;
export const MAX_NAME_CHARS = 200;
export const MAX_NOTE_CHARS = 1000;

export const ORG_HREF = "/org" as Route;

export const orgKeys = {
  all: ["org"] as const,
  chart: ["org", "chart"] as const,
  runs: ["org", "runs"] as const,
  users: ["org", "users"] as const,
  autonomy: ["org", "autonomy"] as const,
  preview: (target: StopSet) => ["org", "stop-preview", target.scope, target.login ?? "", target.unit ?? ""] as const,
};

export const orgQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: orgKeys.chart,
    queryFn: ({ signal }) => call(api().GET("/v1/org", { signal })),
  });

export const orgRunsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: orgKeys.runs,
    queryFn: ({ signal }) => call(api().GET("/v1/org/runs", { signal })),
    refetchInterval: 30_000,
  });

/** The hub's people with their grants; a hub admin's read alone. */
export const orgUsersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: orgKeys.users,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/users", { signal })),
  });

/** Every seat's effective mode of each class and the stops that hold (plan autonomy-levels); anyone signed in. */
export const orgAutonomyQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: orgKeys.autonomy,
    queryFn: ({ signal }) => call(api().GET("/v1/autonomy/org", { signal })),
  });

/** How many runs a stop of `target` would cancel or ask to stop, and whether one holds already. */
export const stopPreviewQuery = (api: ApiSource, target: StopSet) =>
  queryOptions({
    queryKey: orgKeys.preview(target),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/autonomy/stops/preview", { params: { query: { scope: target.scope, login: target.login ?? undefined, unit: target.unit ?? undefined } }, signal })),
    staleTime: 0,
  });

// Writes, each with the session's X-Evo-CSRF header.

export type OrgWrite =
  | { kind: "createUnit"; body: UnitCreate }
  | { kind: "editUnit"; slug: string; body: UnitEdit }
  | { kind: "archiveUnit"; slug: string }
  | { kind: "addSeat"; body: SeatAdd }
  | { kind: "editSeat"; id: number; body: SeatEdit }
  | { kind: "endSeat"; id: number }
  | { kind: "setCeiling"; body: CeilingSet }
  | { kind: "stop"; body: StopSet }
  | { kind: "lift"; body: StopSet };

export async function writeOrg(api: ApiClient, write: OrgWrite): Promise<unknown> {
  const headers = await csrfHeaders(api);
  switch (write.kind) {
    case "createUnit":
      return call(api.POST("/v1/org/units", { body: write.body, headers }));
    case "editUnit":
      return call(api.PATCH("/v1/org/units/{slug}", { params: { path: { slug: write.slug } }, body: write.body, headers }));
    case "archiveUnit":
      return call(api.POST("/v1/org/units/{slug}/archive", { params: { path: { slug: write.slug } }, headers }));
    case "addSeat":
      return call(api.POST("/v1/org/seats", { body: write.body, headers }));
    case "editSeat":
      return call(api.PATCH("/v1/org/seats/{seat_id}", { params: { path: { seat_id: write.id } }, body: write.body, headers }));
    case "endSeat":
      return call(api.POST("/v1/org/seats/{seat_id}/end", { params: { path: { seat_id: write.id } }, headers }));
    case "setCeiling":
      return call(api.PUT("/v1/org/ceilings", { body: write.body, headers }));
    case "stop":
      return call(api.POST("/v1/autonomy/stops", { body: write.body, headers }));
    case "lift":
      return call(api.POST("/v1/autonomy/stops/lift", { body: write.body, headers }));
  }
}
