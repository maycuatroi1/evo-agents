/**
 * Where a run's references lead on the web: a repo's page on its forge, read from the repo's origin as the project
 * registered it (GET /v1/projects/{project}), its branches and commits on GitHub and GitLab, and the http and https
 * addresses in the trace and the result. Only http and https ever become a link: an origin or a piece of text that
 * is anything else (javascript:, data:, file:, a path, a string with spaces or quotes) stays text, since a wrong link
 * is worse than none.
 */

export type Forge = "github" | "gitlab";

/** A repo's page on the web, and the forge whose paths its branches and commits follow (null: another host). */
export type RepoWeb = { url: string; forge: Forge | null };

/** A host name of at least two labels (a public or company host, an IPv4 address); never `localhost` or `C`. */
const HOST = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
/** One part of a repo's path: what GitHub and GitLab allow in a group, an owner or a repo's name. */
const SEGMENT = /^[A-Za-z0-9._~-]+$/;
/** Characters no origin carries: white space, controls, quotes and the ones a URL must escape. */
const STRANGE = /[\s\u0000-\u001f\u007f<>"'`\\{}|^]/;
/** git's scp form, `[user@]host:path`, told apart from a URL by having no `://`. */
const SCP = /^(?:[^@/:\s]+@)?([^@/:\s]+):(?!\/\/)(.+)$/;
/** A `.` or `..` part of a path, written or escaped, which a URL parser would resolve into another page. */
const DOT_SEGMENT = /(?:^|[/:])(?:\.|%2e){1,2}(?:[/?#]|$)/i;
const COMMIT = /^[0-9a-f]{7,64}$/i;

function forgeOf(host: string, segments: readonly string[]): Forge | null {
  if (host === "github.com") return segments.length === 2 ? "github" : null;
  // As the Curator's links do (curator/model.ts, codeUrl): a host whose name says gitlab, gitlab.com or a company's.
  return /gitlab/.test(host) ? "gitlab" : null;
}

/**
 * The web page of a repo from its origin: an https or http origin without its user, its `.git` and its trailing
 * slash; an SSH one (`git@host:owner/repo.git`, `ssh://git@host[:port]/owner/repo.git`) as https on the same host,
 * without the SSH port. Null for no origin, another scheme, or an origin with characters no forge path holds.
 */
export function repoWeb(origin: string | null | undefined): RepoWeb | null {
  const text = origin?.trim() ?? "";
  if (!text || STRANGE.test(text) || DOT_SEGMENT.test(text)) return null;
  let scheme = "https";
  let host: string;
  let port = "";
  let path: string;
  const scp = text.includes("://") ? null : SCP.exec(text);
  if (scp) {
    host = scp[1];
    path = scp[2];
  } else {
    let url: URL;
    try {
      url = new URL(text);
    } catch {
      return null;
    }
    if (url.search || url.hash) return null;
    if (url.protocol === "https:" || url.protocol === "http:") {
      scheme = url.protocol.slice(0, -1);
      port = url.port ? `:${url.port}` : "";
    } else if (url.protocol !== "ssh:" && url.protocol !== "git+ssh:") {
      return null;
    }
    host = url.hostname;
    path = url.pathname;
  }
  host = host.toLowerCase();
  if (host === "www.github.com") host = "github.com";
  if (!HOST.test(host)) return null;
  const segments = path.replace(/^\/+|\/+$/g, "").replace(/\.git$/, "").split("/");
  if (segments.some((segment) => !SEGMENT.test(segment) || segment === "." || segment === "..")) return null;
  return { url: `${scheme}://${host}${port}/${segments.join("/")}`, forge: forgeOf(host, segments) };
}

/** Whether `name` is a branch git could have made: no spaces, controls, `..`, `@{`, `~^:?*[\`, nor a `.lock` part. */
function isBranchName(name: string): boolean {
  if (!name || name.startsWith("-") || name.startsWith("/") || name.endsWith("/") || name.endsWith(".")) return false;
  if (/[\s\u0000-\u001f\u007f~^:?*[\\]|\.\.|@\{|\/\/|\/\.|^\./.test(name)) return false;
  return !name.split("/").some((part) => part.endsWith(".lock"));
}

/** A branch's page on GitHub (`/tree/`) or GitLab (`/-/tree/`); null on another forge or for a name git refuses. */
export function branchUrl(repo: RepoWeb | null, branch: string | null | undefined): string | null {
  if (!repo?.forge || !branch || !isBranchName(branch)) return null;
  const path = branch.split("/").map(encodeURIComponent).join("/");
  return `${repo.url}/${repo.forge === "gitlab" ? "-/tree" : "tree"}/${path}`;
}

/** A commit's page on GitHub (`/commit/`) or GitLab (`/-/commit/`); null on another forge or for anything but hex. */
export function commitUrl(repo: RepoWeb | null, sha: string | null | undefined): string | null {
  if (!repo?.forge || !sha || !COMMIT.test(sha)) return null;
  return `${repo.url}/${repo.forge === "gitlab" ? "-/commit" : "commit"}/${sha.toLowerCase()}`;
}

/** `text` as a link target when it is a whole http or https address with a host; null for anything else. */
export function webUrl(text: string | null | undefined): string | null {
  const value = text?.trim() ?? "";
  if (!/^https?:\/\//i.test(value) || STRANGE.test(value)) return null;
  try {
    const url = new URL(value);
    return (url.protocol === "https:" || url.protocol === "http:") && url.hostname ? url.href : null;
  } catch {
    return null;
  }
}

export type TextPiece = { text: string; href: string | null };

/** An http or https address in running text, up to a space, a quote or an angle bracket. */
const URL_IN_TEXT = /\bhttps?:\/\/[^\s<>"'`{}|\\^]+/gi;
/** Punctuation that ends a sentence rather than the address before it. */
const TRAILING = /[.,;:!?]$/;

/** Takes back what follows an address in prose: a full stop, a comma, a closing bracket the address did not open. */
function trimAddress(candidate: string): string {
  let value = candidate;
  for (;;) {
    if (TRAILING.test(value)) value = value.slice(0, -1);
    else if (value.endsWith(")") && count(value, "(") < count(value, ")")) value = value.slice(0, -1);
    else if (value.endsWith("]") && count(value, "[") < count(value, "]")) value = value.slice(0, -1);
    else return value;
  }
}

function count(text: string, char: string): number {
  return text.split(char).length - 1;
}

/**
 * `text` cut into pieces, each http or https address a piece with its `href` and the words between them pieces
 * without one. Joined back, the pieces are `text` exactly.
 */
export function splitLinks(text: string): TextPiece[] {
  const pieces: TextPiece[] = [];
  let last = 0;
  for (const match of text.matchAll(URL_IN_TEXT)) {
    const address = trimAddress(match[0]);
    const href = webUrl(address);
    if (!href) continue;
    const start = match.index;
    if (start > last) pieces.push({ text: text.slice(last, start), href: null });
    pieces.push({ text: address, href });
    last = start + address.length;
  }
  if (last < text.length || pieces.length === 0) pieces.push({ text: text.slice(last), href: null });
  return pieces;
}
