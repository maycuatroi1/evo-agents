import type { BrowserIdentity, Worker } from "./queries";

/** `credentials.SECRET_NAME` of the hub: lower case. */
export const IDENTITY_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;
/** A domain as the hub takes it: labels of letters, digits and hyphens, with at least one dot or localhost. */
export const DOMAIN = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/i;

/** The domains typed in a field, split on commas, spaces and newlines, empty ones and repeats left out. */
export function parseDomains(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\s,]+/)) {
    const domain = part.trim().toLowerCase();
    if (domain) seen.add(domain);
  }
  return [...seen];
}

/** Whether `text` is an http or https address with a host, as a login session opens it. */
export function validLoginUrl(text: string): boolean {
  try {
    const url = new URL(text);
    return (url.protocol === "https:" || url.protocol === "http:") && url.hostname !== "";
  } catch {
    return false;
  }
}

/**
 * The workers an identity may be held by, from the visitor's list: not revoked, serving the project (a shared one of
 * another member serves it too), reporting the feature browser. The visitor's own come first, then the shared ones,
 * each by name.
 */
export function browserWorkers(workers: readonly Worker[], project: string, login: string | null): Worker[] {
  const usable = workers.filter(
    (worker) => worker.status !== "revoked" && (worker.features ?? []).includes("browser") && (worker.owner === login || worker.projects.includes(project)),
  );
  const rank = (worker: Worker) => (worker.owner === login ? 0 : 1);
  return usable.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

/** Whether a login session may be asked for the identity now: it is not disabled and no run holds it. */
export function canLogin(identity: Pick<BrowserIdentity, "status" | "held_by_run">): boolean {
  return identity.status !== "disabled" && identity.held_by_run === null;
}
