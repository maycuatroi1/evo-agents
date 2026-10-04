import type { components } from "@/lib/api/schema";

/** The knowledge graph's API types, as the hub's OpenAPI document names them (`pnpm gen:api`). */
type Schemas = components["schemas"];

export type KgBuild = Schemas["Build"];
export type KgBuilds = Schemas["Builds"];
export type KgJob = Schemas["Job"];
export type BuildStatus = KgBuild["status"];
export type GraphRef = Schemas["GraphRef"];
export type GraphSummary = Schemas["GraphSummary"];
export type KindCount = Schemas["KindCount"];
export type KgNode = Schemas["KgNode"];
export type NodeLabel = Schemas["NodeLabel"];
export type NodeSearch = Schemas["NodeSearch"];
export type NodeDetail = Schemas["NodeDetail"];
export type Relation = Schemas["Relation"];
export type Evidence = Schemas["Evidence"];
export type Neighbourhood = Schemas["Neighbourhood"];
export type GraphNode = Schemas["GraphNode"];
export type GraphEdge = Schemas["GraphEdge"];

/** The neighbourhood's bounds, as the API enforces them (`evo_agents.hub.kg_web`). */
export const MAX_HOPS = 2;
export const MAX_NODES = 150;
export type Hops = 1 | 2;
