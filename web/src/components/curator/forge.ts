import { webBase } from "./model";
import type { Change } from "./queries";

/**
 * Where a proposal's change card links a change's branch on its forge: GitHub (`/tree/`) or GitLab (`/-/tree/`), from
 * the repo's web page that `webBase` (model.ts) reads from its origin, as `codeUrl` there links a line of code. A
 * module of its own, so the Curator's pages that load model.ts do not load what only the change card uses.
 */

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
