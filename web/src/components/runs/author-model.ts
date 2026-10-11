import { type FitRequest, fitWorker } from "./model";
import { MAX_REQUEST_BYTES } from "./queries";

type Worker = Parameters<typeof fitWorker>[0];

/**
 * How the web reads an author run before it is dispatched: the checkout of the project's harness it needs, the worker
 * it goes to, and the member's request. Pure functions, shared by the author run dialog and its tests.
 */

// Author runs. An author run is one run on a worker of the member's, always pinned, that writes a plan from their
// request (runs.py, dispatch_author): its first repo is the project's harness, where plans live, which the worker must
// have a checkout of, and it runs on Claude Code alone.

/**
 * The name a worker gives the checkout of a project's harness: the last part of its path, as the hub's
 * `author.harness_repo` reads it; null for a project registered without its harness.
 */
export function harnessRepo(harness: { path: string } | null | undefined): string | null {
  if (!harness || typeof harness.path !== "string") return null;
  const name = harness.path.replaceAll("\\", "/").replace(/\/+$/, "").split("/").pop() ?? "";
  return name === "" || name === "." || name === ".." || name === "~" ? null : name;
}

/** Whether `worker` could claim an author run of `request` now. */
export function workerFitsNow(worker: Worker, request: FitRequest): boolean {
  return fitWorker(worker, request).fit === "now";
}

/**
 * The worker an author run goes to: the one picked while it is still among `workers`, else the first that could take
 * the run now, else the first that could take it at all, else the first; null without a worker.
 */
export function authorWorker(workers: readonly Worker[], request: FitRequest, picked: number | null): number | null {
  if (picked !== null && workers.some((worker) => worker.id === picked)) return picked;
  const fits = workers.map((worker) => fitWorker(worker, request));
  const best = fits.find((fit) => fit.fit === "now") ?? fits.find((fit) => fit.fit !== "no") ?? fits[0];
  return best ? best.worker.id : null;
}

/** Why a request cannot go with an author run: nothing but blanks, or over 16 KiB of UTF-8. */
export type RequestProblem = "blank" | "long";

/** The request as the API takes it (as typed: the agent reads the member's own words), or the problem with it. */
export function readRequest(text: string): { request: string; problem: RequestProblem | null } {
  if (!text.trim()) return { request: text, problem: "blank" };
  if (new TextEncoder().encode(text).length > MAX_REQUEST_BYTES) return { request: text, problem: "long" };
  return { request: text, problem: null };
}
