import type { Worker } from "./queries";

/** The feature a daemon says once Chromium is installed (`runs.BROWSER` of the hub). */
export const BROWSER_FEATURE = "browser";
/** The feature a daemon says when it installs Chromium on its owner's ask (`runs.BROWSER_INSTALL` of the hub). */
export const BROWSER_INSTALL_FEATURE = "browser_install";

type Offered = Pick<Worker, "status" | "features" | "browser" | "browser_install">;

/** The worker's Install browser while it is open: asked for and not yet ended. */
export function openBrowserInstall(worker: Pick<Worker, "browser_install">): Worker["browser_install"] {
  const request = worker.browser_install;
  return request && request.ended_at === null ? request : null;
}

/**
 * What the page offers of Install browser. `installed`: the worker's heartbeat lists the feature browser. `open`: a
 * request waits, for the worker to come back (`offline`) or for its daemon to install. `ready`: no Chromium, and the
 * owner may ask. `blocked`: no Chromium, but the hub would refuse the ask: the daemon is too old, or the worker revoked.
 */
export type BrowserInstallOffer =
  | { kind: "installed" }
  | { kind: "open"; phase: "offline" | "installing" }
  | { kind: "ready" }
  | { kind: "blocked"; why: "tooOld" | "revoked" };

export function browserInstallOffer(worker: Offered): BrowserInstallOffer {
  const features = worker.features ?? [];
  if (features.includes(BROWSER_FEATURE) && worker.browser) return { kind: "installed" };
  if (worker.status === "revoked") return { kind: "blocked", why: "revoked" };
  if (openBrowserInstall(worker)) return { kind: "open", phase: worker.status === "offline" ? "offline" : "installing" };
  if (!features.includes(BROWSER_INSTALL_FEATURE)) return { kind: "blocked", why: "tooOld" };
  return { kind: "ready" };
}

/** Why the last request ended without Chromium, as the daemon or the hub said; null when it did not, or is open. */
export function lastInstallProblem(worker: Pick<Worker, "browser_install">): { outcome: "failed" | "impossible"; reason: string } | null {
  const last = worker.browser_install;
  if (!last || last.ended_at === null || !last.reason) return null;
  return last.outcome === "failed" || last.outcome === "impossible" ? { outcome: last.outcome, reason: last.reason } : null;
}
