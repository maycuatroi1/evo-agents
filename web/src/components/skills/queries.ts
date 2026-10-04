import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of the skills pages. Global skills are every signed-in member's; a project's are its members' (the API
 * answers 403 to anyone else, the same whether the project exists or not). Skills carry no label: the grant alone
 * decides.
 */
type Schemas = components["schemas"];
export type Skill = Schemas["Skill"];
export type SkillHistory = Schemas["SkillHistory"];
export type SkillVersion = Schemas["Version"];
export type BundleTicket = Schemas["BundleTicket"];

/** Where skills live: the hub's global ones, or one project's. */
export type SkillPlace = { kind: "global" } | { kind: "project"; project: string };

function placeKey(place: SkillPlace): string {
  return place.kind === "project" ? `project:${place.project}` : "global";
}

export const skillKeys = {
  list: (place: SkillPlace) => ["skills", "list", placeKey(place)] as const,
  one: (place: SkillPlace, name: string) => ["skills", "one", placeKey(place), name] as const,
  blobStore: ["health", "blob-store"] as const,
};

export const skillsQuery = (api: ApiSource, place: SkillPlace) =>
  queryOptions({
    queryKey: skillKeys.list(place),
    queryFn: ({ signal }) => {
      const query =
        place.kind === "project" ? { scope: "project" as const, project: place.project } : { scope: "global" as const };
      return call(api().GET("/v1/skills", { params: { query }, signal }));
    },
  });

export const skillQuery = (api: ApiSource, place: SkillPlace, name: string) =>
  queryOptions({
    queryKey: skillKeys.one(place, name),
    queryFn: ({ signal }) =>
      place.kind === "project"
        ? call(
            api().GET("/v1/skills/projects/{project}/{name}", {
              params: { path: { project: place.project, name } },
              signal,
            }),
          )
        : call(api().GET("/v1/skills/global/{name}", { params: { path: { name } }, signal })),
  });

/** A presigned GET of one version's bundle: asked for when the visitor clicks, never prefetched (it expires). */
export function bundleTicket(api: ApiSource, place: SkillPlace, name: string, version: number) {
  const query = { version };
  return place.kind === "project"
    ? call(
        api().GET("/v1/skills/projects/{project}/{name}/bundle", {
          params: { path: { project: place.project, name }, query },
        }),
      )
    : call(api().GET("/v1/skills/global/{name}/bundle", { params: { path: { name }, query } }));
}

export type BlobStoreState = "ok" | "unavailable" | "unconfigured" | "unknown";

/**
 * Whether the hub can hand out bundles at all, from its public health check: "unconfigured" when it runs without a
 * blob store, so the page can say so before anyone clicks. A failed check is "unknown", never a reason to refuse.
 */
export const blobStoreQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: skillKeys.blobStore,
    queryFn: async ({ signal }): Promise<BlobStoreState> => {
      try {
        const { data, error } = await api().GET("/v1/health", { signal });
        return (data ?? error)?.r2 ?? "unknown";
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
        return "unknown";
      }
    },
    staleTime: 60_000,
  });

/** A skill name as the API accepts it (`SKILL_NAME`). */
export const SKILL_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;

export function skillsHref(place: SkillPlace): Route {
  return place.kind === "project" ? projectHref(place.project, "skills") : ("/skills" as Route);
}

export function skillHref(place: SkillPlace, name: string): Route {
  const segment = `skills/${encodeURIComponent(name)}`;
  return place.kind === "project" ? projectHref(place.project, segment) : (`/${segment}` as Route);
}
