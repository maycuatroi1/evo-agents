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
export type SourceRead = Schemas["SourceRead"];
export type KgNode = Schemas["KgNode"];
export type NodeLabel = Schemas["NodeLabel"];
export type NodeSearch = Schemas["NodeSearch"];
export type NodeDetail = Schemas["NodeDetail"];
export type Relation = Schemas["Relation"];
export type Evidence = Schemas["Evidence"];
export type Neighbourhood = Schemas["Neighbourhood"];
export type GraphNode = Schemas["GraphNode"];
export type GraphEdge = Schemas["GraphEdge"];
export type NeighbourGroup = Schemas["NeighbourGroup"];
export type GraphShape = Schemas["GraphShape"];
export type ShapeRow = Schemas["ShapeRow"];
export type ClusterMap = Schemas["ClusterMap"];
export type ClusterSummary = Schemas["ClusterSummary"];
export type ClusterLink = Schemas["ClusterLink"];
export type MapNode = Schemas["MapNode"];
export type ClusterDetail = Schemas["ClusterDetail"];
export type ClusterNeighbour = Schemas["ClusterNeighbour"];
export type WholeGraph = Schemas["WholeGraph"];
export type WholeNode = Schemas["WholeNode"];
export type ReviewQueue = Schemas["ReviewQueue"];
export type ReviewItem = Schemas["ReviewItem"];
export type ReviewNode = Schemas["ReviewNode"];
export type RuleCount = Schemas["RuleCount"];
export type Dismissal = Schemas["Dismissal"];
export type ReviewRule = ReviewItem["rule"];
export type KgDecision = Schemas["KgDecision"];
export type KgDecisionEvent = Schemas["KgDecisionEvent"];
export type KgDecisionList = Schemas["KgDecisionList"];
export type NewKgDecision = Schemas["NewKgDecision"];
export type DecisionKind = KgDecision["kind"];
export type DecisionState = KgDecision["state"];

/** The neighbourhood's bounds, as the API enforces them (`evo_agents.hub.kg_web`). */
export const MAX_HOPS = 2;
export const MAX_NODES = 150;
export type Hops = 1 | 2;
/** Direct neighbours of each group the node page shows before the group is opened (`GROUP_SHOWN` of the API). */
export const GROUP_SHOWN = 5;
