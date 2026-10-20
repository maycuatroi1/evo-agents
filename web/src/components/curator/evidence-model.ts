import type { Route } from "next";

/**
 * What a piece of evidence of a finding or a proposal names, as the hub resolved it (evo_agents.hub.review), and the
 * forge's addresses of a repo's files; `evidenceView` of model.ts says where each piece leads.
 */

/** A repo of the project as GET /v1/projects/{p} lists it. */
export type RepoInfo = { name: string; origin?: string | null; default_branch?: string | null };

export type EvidenceView =
  | { kind: "run"; runId: number; seq: number; href: Route }
  | { kind: "session"; session: string; field: string | null; index: number | null }
  | { kind: "transcript"; session: string; file: string; line: number }
  | { kind: "code"; repo: string; path: string; line: number | null; commit: string | null; url: string | null }
  | { kind: "other"; text: string };

/** A repo's https web page from its origin (https, http or git's scp form), else null; `branchUrl` (forge.ts) uses it. */
export function webBase(origin: string | null | undefined): string | null {
  const scp = /^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?\/?$/.exec(origin ?? "");
  if (scp) return `https://${scp[1]}/${scp[2]}`;
  try {
    const url = new URL(origin ?? "");
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return `https://${url.host}${url.pathname.replace(/\.git\/?$/, "").replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

/**
 * The web address of a file of a repo at `ref`, on the line given: GitHub's blob URL, or GitLab's for a host whose
 * name says gitlab. Null for an origin the web cannot read as https or as git's scp form (`git@host:group/repo.git`).
 */
export function codeUrl(origin: string | null | undefined, ref: string | null, path: string, line: number | null): string | null {
  const base = webBase(origin);
  if (!base || !ref) return null;
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const blob = /gitlab/i.test(new URL(base).host) ? "-/blob" : "blob";
  return `${base}/${blob}/${encodeURIComponent(ref)}/${encoded}${line !== null ? `#L${line}` : ""}`;
}

/** The entry of a digest that a piece of evidence names, as text: a line the person typed, a command, an error. */
export function digestEntry(digest: Record<string, unknown>, field: string | null, index: number | null): string | null {
  if (field === null || index === null) return null;
  const list = digest[field];
  if (!Array.isArray(list) || index < 0 || index >= list.length) return null;
  const entry: unknown = list[index];
  if (typeof entry === "string") return entry;
  return JSON.stringify(entry, null, 2);
}
