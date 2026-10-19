import type { ItemKind, ReviewRole, SrsComment, SrsDocument, SrsItem, SrsReview, SrsVersion } from "./api";

/**
 * The pure model of the SRS pages: how a version's items form the document's outline, how an item is found by its
 * code, key or alias (as the hub compares codes, evo_agents.hub.tables_srs.code_key), which version a page shows, who
 * may review a version (evo_agents.core_plugins.srs.reviews) and what a use case holds. Nothing here reads the API.
 */

export const ROLES: readonly ReviewRole[] = ["ba", "qa", "dev"];

/** The kinds whose items carry a code that never changes, and so have a page of their own. */
export const CODED_KINDS: ReadonlySet<ItemKind> = new Set(["use_case", "business_rule", "screen", "nfr", "cbr"]);

/** The node of the knowledge graph each kind of coded item is (evo_agents.hub.tables_srs.KG_NODE_KINDS). */
const KG_NODE: Partial<Record<ItemKind, string>> = {
  use_case: "usecase",
  business_rule: "requirement",
  nfr: "requirement",
  cbr: "requirement",
};

/** A code as the hub compares two: whitespace runs as one space, case folded. */
export function codeKey(code: string): string {
  return code.trim().split(/\s+/).join(" ").toLowerCase();
}

/** The id of an item's node in the knowledge graph (`usecase:UC ORD-1`), or null for a kind the graph has no node for. */
export function kgNodeId(item: Pick<SrsItem, "kind" | "code">): string | null {
  const node = KG_NODE[item.kind];
  const code = item.code?.trim();
  return node && code ? `${node}:${code.split(/\s+/).join(" ")}` : null;
}

/** The item `ref` names in `items`: its key, its code or one of its aliases; null when none does. */
export function findItem(items: readonly SrsItem[], ref: string): SrsItem | null {
  const byKey = items.find((item) => item.key === ref);
  if (byKey) return byKey;
  const wanted = codeKey(ref);
  return items.find((item) => [item.code, ...item.aliases].some((code) => code && codeKey(code) === wanted)) ?? null;
}

/** Whether an item is a table row (an NFR, a common rule) rather than a heading of its own. */
export function isRow(item: Pick<SrsItem, "heading_level">): boolean {
  return item.heading_level === null;
}

export function childrenOf(items: readonly SrsItem[], key: string): SrsItem[] {
  return items.filter((item) => item.parent === key);
}

/** The items above `key`, the top first. */
export function pathOf(items: readonly SrsItem[], key: string): SrsItem[] {
  const byKey = new Map(items.map((item) => [item.key, item]));
  const path: SrsItem[] = [];
  let parent = byKey.get(key)?.parent ?? null;
  while (parent !== null && path.length < items.length) {
    const above = byKey.get(parent);
    if (!above) break;
    path.unshift(above);
    parent = above.parent;
  }
  return path;
}

export type OutlineNode = { item: SrsItem; children: OutlineNode[] };

/** The document's headings as a tree, in the version's order; table rows stay inside their section. */
export function outline(items: readonly SrsItem[]): OutlineNode[] {
  const headings = items.filter((item) => !isRow(item));
  const nodes = new Map(headings.map((item) => [item.key, { item, children: [] as OutlineNode[] }]));
  const roots: OutlineNode[] = [];
  for (const item of headings) {
    const node = nodes.get(item.key)!;
    const parent = item.parent === null ? undefined : nodes.get(item.parent);
    (parent ? parent.children : roots).push(node);
  }
  return roots;
}

/** The item a document page opens without `s`: the first that holds text, past the approval page and the history. */
export function firstItem(items: readonly SrsItem[]): SrsItem | null {
  const reading = items.filter((item) => item.kind !== "approval_page" && item.kind !== "revision_history");
  return reading.find((item) => item.body.trim() !== "" || CODED_KINDS.has(item.kind)) ?? reading[0] ?? items[0] ?? null;
}

// Versions

/** The version a page shows: the one asked for when the document has it, else the approved one, else the latest. */
export function shownNumber(doc: Pick<SrsDocument, "versions" | "approved_version" | "latest_version">, asked: number | null) {
  if (asked !== null && doc.versions.some((version) => version.number === asked)) return asked;
  return doc.approved_version ?? doc.latest_version;
}

/** A version number from a URL parameter: a whole number of at least 1, else null. */
export function versionParam(value: string | string[] | null | undefined): number | null {
  const text = Array.isArray(value) ? value[0] : value;
  if (!text || !/^[1-9][0-9]{0,8}$/.test(text)) return null;
  return Number(text);
}

export function versionOf(doc: Pick<SrsDocument, "versions">, number: number | null): SrsVersion | null {
  return number === null ? null : (doc.versions.find((version) => version.number === number) ?? null);
}

/** The pair a comparison shows: the one asked when both exist and differ, else the approved version and the latest. */
export function comparePair(doc: Pick<SrsDocument, "versions" | "approved_version" | "latest_version">, from: number | null, to: number | null) {
  const has = (number: number | null) => number !== null && doc.versions.some((version) => version.number === number);
  if (has(from) && has(to) && from !== to) return { from: Math.min(from!, to!), to: Math.max(from!, to!) };
  const latest = doc.latest_version;
  if (latest === null) return null;
  const older = doc.approved_version && doc.approved_version !== latest ? doc.approved_version : (doc.versions.find((version) => version.number < latest)?.number ?? null);
  return older === null ? null : { from: older, to: latest };
}

/** The version waiting for review, newest first; a document has at most one in practice. */
export function inReview(doc: Pick<SrsDocument, "versions">): SrsVersion | null {
  return doc.versions.find((version) => version.state === "in_review") ?? null;
}

/** Who wrote a version after the one approved last, up to `number`: none of them reviews it. */
export function authorsSince(doc: Pick<SrsDocument, "versions" | "approved_version">, number: number): Set<string> {
  const since = doc.approved_version ?? 0;
  return new Set(doc.versions.filter((v) => v.number > since && v.number <= number).map((v) => v.author));
}

/** The reviews of the round a version is in now. */
export function currentRound(version: SrsVersion): SrsReview[] {
  return version.reviews.filter((review) => review.round === version.round);
}

/** The request for changes that sent a draft back, when the last round ended with one. */
export function returnedBy(version: SrsVersion): SrsReview | null {
  if (version.state !== "draft" || version.round === 0) return null;
  const asked = currentRound(version).filter((review) => review.decision === "request_changes");
  return asked.at(-1) ?? null;
}

export type Viewer = { login: string; role: "reader" | "writer" | "admin" | null };

export function canWrite(viewer: Viewer | null): boolean {
  return viewer?.role === "writer" || viewer?.role === "admin";
}

/** Why `viewer` cannot review `version` now, as the hub would refuse it; null when they can. */
export type ReviewBlock = "reader" | "not_in_review" | "author" | "reviewed";

export function reviewBlock(
  viewer: Viewer,
  doc: Pick<SrsDocument, "versions" | "approved_version">,
  version: SrsVersion,
): ReviewBlock | null {
  if (!canWrite(viewer)) return "reader";
  if (version.state !== "in_review") return "not_in_review";
  if (authorsSince(doc, version.number).has(viewer.login)) return "author";
  if (currentRound(version).some((review) => review.reviewer === viewer.login)) return "reviewed";
  return null;
}

// The plugin's config: roles and the approval policy

export type Policy = { required: ReviewRole[]; optional: ReviewRole[] };
type ConfigField = { name: string; value?: unknown };

function roleList(value: unknown): ReviewRole[] {
  return Array.isArray(value) ? ROLES.filter((role) => value.includes(role)) : [];
}

/** The policy of a kind of document in the effective config; none required means one approval is enough. */
export function policyOf(fields: readonly ConfigField[], kind: string): Policy {
  const policies = fields.find((field) => field.name === "approval_policy")?.value;
  const policy = policies && typeof policies === "object" ? (policies as Record<string, unknown>)[kind] : undefined;
  const held = policy && typeof policy === "object" ? (policy as Record<string, unknown>) : {};
  return { required: roleList(held.required), optional: roleList(held.optional) };
}

/** The roles `login` holds in the config's `roles`. */
export function rolesOf(fields: readonly ConfigField[], login: string): ReviewRole[] {
  const roles = fields.find((field) => field.name === "roles")?.value;
  if (!roles || typeof roles !== "object") return [];
  const held = roles as Record<string, unknown>;
  return ROLES.filter((role) => Array.isArray(held[role]) && (held[role] as unknown[]).includes(login));
}

export type Slot = { role: ReviewRole; required: boolean; by: SrsReview | null };

/** Each role the policy names, required ones first, with the approval of this round that filled it. */
export function slots(policy: Policy, version: SrsVersion): Slot[] {
  const approvals = currentRound(version).filter((review) => review.decision === "approve");
  const slot = (role: ReviewRole, required: boolean) => ({ role, required, by: approvals.find((r) => r.role === role) ?? null });
  return [...policy.required.map((role) => slot(role, true)), ...policy.optional.map((role) => slot(role, false))];
}

// Comments

export type Thread = { root: SrsComment; replies: SrsComment[] };

/** The comments of a document as threads, oldest first: each root comment with every answer under it. */
export function threadsOf(comments: readonly SrsComment[]): Thread[] {
  const byId = new Map(comments.map((comment) => [comment.id, comment]));
  const rootOf = (comment: SrsComment): SrsComment => {
    let at = comment;
    for (let hops = 0; at.parent_id !== null && hops < comments.length; hops += 1) at = byId.get(at.parent_id) ?? at;
    return at;
  };
  const threads = new Map<number, Thread>();
  for (const comment of comments) {
    if (comment.parent_id === null) threads.set(comment.id, { root: comment, replies: [] });
  }
  for (const comment of comments) {
    if (comment.parent_id !== null) threads.get(rootOf(comment).id)?.replies.push(comment);
  }
  return [...threads.values()];
}
