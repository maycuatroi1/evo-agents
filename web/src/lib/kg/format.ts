import type { KgBuild } from "./types";

/**
 * Times of a build: how long it waited in the queue and how long it ran, in milliseconds. A build still waiting or
 * running counts up to `now`; without `now` (the server's render, before the browser's clock takes over) its open
 * phase is null rather than a number that would differ between the two renders.
 */
export type BuildDurations = { wait: number | null; run: number | null };

function time(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

const positive = (ms: number) => Math.max(0, ms);

export function buildDurations(
  build: Pick<KgBuild, "status" | "queued_at" | "started_at" | "finished_at">,
  now: number | null,
): BuildDurations {
  const queued = time(build.queued_at);
  const started = time(build.started_at);
  const finished = time(build.finished_at);
  let wait: number | null = null;
  if (queued !== null && started !== null) wait = positive(started - queued);
  else if (queued !== null && build.status === "queued" && now !== null) wait = positive(now - queued);
  let run: number | null = null;
  if (started !== null && finished !== null) run = positive(finished - started);
  else if (started !== null && build.status === "running" && now !== null) run = positive(now - started);
  return { wait, run };
}

/** A duration in the largest units that matter, for the messages under `kg.duration`. */
export type DurationParts =
  | { unit: "underSecond" }
  | { unit: "seconds"; seconds: number }
  | { unit: "minutes"; minutes: number; seconds: number }
  | { unit: "hours"; hours: number; minutes: number };

export function durationParts(ms: number): DurationParts {
  const total = Math.floor(Math.max(0, ms) / 1000);
  if (total < 1) return { unit: "underSecond" };
  if (total < 60) return { unit: "seconds", seconds: total };
  if (total < 3600) return { unit: "minutes", minutes: Math.floor(total / 60), seconds: total % 60 };
  return { unit: "hours", hours: Math.floor(total / 3600), minutes: Math.floor((total % 3600) / 60) };
}

/** `sha256:` and the first `length` hex digits: enough to tell builds apart at a glance. */
export function shortHash(hash: string | null | undefined, length = 12): string {
  if (!hash) return "-";
  const hex = hash.startsWith("sha256:") ? hash.slice("sha256:".length) : hash;
  return hex.length > length ? `${hex.slice(0, length)}…` : hex;
}

/** Whether a URI can be opened as a link from the page: http and https only, never javascript: or file:. */
export function isWebLink(uri: string | null | undefined): uri is string {
  if (!uri) return false;
  try {
    const url = new URL(uri);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}
