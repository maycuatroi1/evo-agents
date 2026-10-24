import { describe, expect, it } from "vitest";

import { browserInstallOffer, lastInstallProblem, openBrowserInstall } from "./browser-install";
import type { Worker } from "./queries";

type Offered = Parameters<typeof browserInstallOffer>[0];

const CHROMIUM = { chromium: "153.0.8010.12", display: "headless", max: 2, sandbox: true } as const;
const ASKED = "2026-10-25T07:00:00Z";

function worker(overrides: Partial<Offered> = {}): Offered {
  return { status: "online", features: ["browser_install"], browser: null, browser_install: null, ...overrides };
}

function request(overrides: Partial<NonNullable<Worker["browser_install"]>> = {}): NonNullable<Worker["browser_install"]> {
  return { requested_at: ASKED, ended_at: null, outcome: null, reason: null, ...overrides };
}

describe("browserInstallOffer", () => {
  it("says installed once the heartbeat lists the feature browser with its Chromium, whatever the last request", () => {
    expect(browserInstallOffer(worker({ features: ["browser_install", "browser"], browser: CHROMIUM }))).toEqual({ kind: "installed" });
    const open = worker({ features: ["browser"], browser: CHROMIUM, browser_install: request() });
    expect(browserInstallOffer(open)).toEqual({ kind: "installed" });
  });

  it("offers the ask to a daemon that says browser_install, and blocks an older one or a revoked worker", () => {
    expect(browserInstallOffer(worker())).toEqual({ kind: "ready" });
    expect(browserInstallOffer(worker({ features: [] }))).toEqual({ kind: "blocked", why: "tooOld" });
    expect(browserInstallOffer(worker({ status: "revoked" }))).toEqual({ kind: "blocked", why: "revoked" });
  });

  it("shows an open request as installing, or waiting while the worker is offline", () => {
    expect(browserInstallOffer(worker({ browser_install: request() }))).toEqual({ kind: "open", phase: "installing" });
    const offline = worker({ status: "offline", browser_install: request() });
    expect(browserInstallOffer(offline)).toEqual({ kind: "open", phase: "offline" });
    const ended = worker({ browser_install: request({ ended_at: ASKED, outcome: "cancelled" }) });
    expect(browserInstallOffer(ended)).toEqual({ kind: "ready" });
  });
});

describe("lastInstallProblem", () => {
  it("names why the last request ended failed or impossible, and nothing for one open, cancelled or installed", () => {
    const failed = request({ ended_at: ASKED, outcome: "failed", reason: "npx is not on PATH" });
    expect(lastInstallProblem({ browser_install: failed })).toEqual({ outcome: "failed", reason: "npx is not on PATH" });
    const impossible = request({ ended_at: ASKED, outcome: "impossible", reason: "too old" });
    expect(lastInstallProblem({ browser_install: impossible })).toEqual({ outcome: "impossible", reason: "too old" });
    expect(lastInstallProblem({ browser_install: request() })).toBeNull();
    expect(lastInstallProblem({ browser_install: request({ ended_at: ASKED, outcome: "installed" }) })).toBeNull();
    expect(lastInstallProblem({ browser_install: null })).toBeNull();
    expect(openBrowserInstall({ browser_install: request({ ended_at: ASKED, outcome: "cancelled" }) })).toBeNull();
  });
});
