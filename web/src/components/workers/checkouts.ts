import type { CheckoutInfo } from "./model";

// The worker page's Checkouts card alone: kept out of model.ts, which the shell and Home share with every page.

/** A checkout as the worker's page lists it: the repository without its project, and where it is when that says more. */
export type CheckoutRow = CheckoutInfo & { repo: string; where: string | null };
export type CheckoutGroup = { project: string | null; rows: CheckoutRow[] };

/** The directory every path sits in, when they are single paths and share one; null otherwise. */
function commonParent(paths: (string | null)[]): string | null {
  if (paths.length < 2) return null;
  let parent: string | null = null;
  for (const path of paths) {
    if (!path || path.includes(", ")) return null;
    const trimmed = path.replace(/\/+$/, "");
    const cut = trimmed.lastIndexOf("/");
    if (cut <= 0) return null;
    const dir = trimmed.slice(0, cut);
    if (parent === null) parent = dir;
    else if (parent !== dir) return null;
  }
  return parent;
}

/**
 * Checkouts by project: a heartbeat names them `project/repo`, a name without a slash goes in a group of its own,
 * first. When every path sits in one directory, `base` names it once and a row keeps only the directory's name, and
 * only where it differs from the repository's.
 */
export function groupCheckouts(checkouts: CheckoutInfo[]): { base: string | null; groups: CheckoutGroup[] } {
  const base = commonParent(checkouts.map((checkout) => checkout.path));
  const groups = new Map<string | null, CheckoutRow[]>();
  for (const checkout of checkouts) {
    const slash = checkout.name.indexOf("/");
    const project = slash > 0 ? checkout.name.slice(0, slash) : null;
    const repo = slash > 0 ? checkout.name.slice(slash + 1) : checkout.name;
    let where = checkout.path;
    if (base && where) {
      const leaf = where.replace(/\/+$/, "").slice(base.length + 1);
      where = leaf === repo ? null : leaf;
    }
    const rows = groups.get(project) ?? [];
    rows.push({ ...checkout, repo, where });
    groups.set(project, rows);
  }
  const ordered = [...groups].map(([project, rows]) => ({ project, rows }));
  return { base, groups: [...ordered.filter((group) => group.project === null), ...ordered.filter((group) => group.project !== null)] };
}

/** The first `limit` rows of the groups, in order; each group keeps its whole count in `total`. */
export function firstCheckouts(groups: CheckoutGroup[], limit: number): (CheckoutGroup & { total: number })[] {
  const shown: (CheckoutGroup & { total: number })[] = [];
  let left = limit;
  for (const group of groups) {
    if (left <= 0) break;
    const rows = group.rows.slice(0, left);
    left -= rows.length;
    shown.push({ project: group.project, total: group.rows.length, rows });
  }
  return shown;
}
