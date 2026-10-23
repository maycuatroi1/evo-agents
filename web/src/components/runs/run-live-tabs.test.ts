import { describe, expect, it } from "vitest";

import { browserAccess } from "./run-live-tabs";

const run = { dispatched_by: "ana", browser: true, kind: "step" as const, state: "running" as const, worker_id: 7 };

describe("browserAccess", () => {
  it("offers the view to the run's dispatcher while a worker holds a run started with a browser", () => {
    for (const state of ["leased", "running", "interactive", "verifying", "waiting"] as const) {
      expect(browserAccess({ ...run, state }, { login: "ana" })).toEqual({ open: true });
    }
  });

  it("keeps it closed once no worker holds the run", () => {
    for (const state of ["queued", "review", "parked", "done", "failed", "lost", "cancelled"] as const) {
      expect(browserAccess({ ...run, state }, { login: "ana" })).toEqual({ open: false });
    }
    expect(browserAccess({ ...run, worker_id: null }, { login: "ana" })).toEqual({ open: false });
  });

  it("hides it from anyone else, from a run without a browser and from a local run", () => {
    expect(browserAccess(run, null)).toBeNull();
    expect(browserAccess(run, { login: "teo" })).toBeNull();
    expect(browserAccess({ ...run, browser: false }, { login: "ana" })).toBeNull();
    expect(browserAccess({ ...run, kind: "local" }, { login: "ana" })).toBeNull();
  });
});
