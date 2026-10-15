import type { Change } from "./queries";

/**
 * Where the Curator's pages link to a repo on its forge, from the origin the project registered: the repo's web page,
 * and a branch of a change on GitHub (`/tree/`) or GitLab (`/-/tree/`). `codeUrl` (model.ts) links a line of code the
 * same way. Only an https or http origin, or git's scp form (`git@host:group/repo.git`), becomes a link; anything else
 * stays text, since a wrong link is worse than none.
 */

/** The https address of a repo's web page, or null for an origin the web cannot read as one. */
export function webBase(origin: string | null | undefined): string | null {
  if (!origin) return null;
  const scp = /^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?\/?$/.exec(origin);
  if (scp) return `https://${scp[1]}/${scp[2]}`;
  try {
    const url = new URL(origin);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return `https://${url.host}${url.pathname.replace(/\.git\/?$/, "").replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

/** Whether a branch name is one git could have made, so its path on the forge is what it says. */
function isBranchName(name: string): boolean {
  if (!name || name.startsWith("-") || name.startsWith("/") || name.endsWith("/") || name.endsWith(".")) return false;
  return !/[\s\u0000-\u001f\u007f~^:?*[\\]|\.\.|@\{|\/\/|\/\./.test(name);
}

/** The page of a change's branch on its forge; null without a forge the hub named, an origin or a branch git takes. */
export function branchUrl(origin: string | null | undefined, branch: string | null, forge: Change["forge"]): string | null {
  const base = webBase(origin);
  if (!base || !branch || !forge || !isBranchName(branch)) return null;
  const path = branch.split("/").map(encodeURIComponent).join("/");
  return `${base}/${forge === "gitlab" ? "-/tree" : "tree"}/${path}`;
}
