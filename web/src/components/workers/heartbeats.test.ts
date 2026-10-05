// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import {
  countCells,
  heartbeatCells,
  heartbeatsVersion,
  missedRuns,
  observationsOf,
  recordHeartbeats,
  resetHeartbeats,
  STRIP_MINUTES,
} from "./heartbeats";

const MINUTE = 60_000;
const T0 = Date.parse("2026-10-05T07:00:00Z"); // a whole minute
const at = (minutes: number, seconds = 0) => T0 + minutes * MINUTE + seconds * 1000;

describe("heartbeatCells", () => {
  it("gives 60 cells, the last one holding now, all unknown without observations", () => {
    const cells = heartbeatCells([], at(-120), at(59, 30));
    expect(cells).toHaveLength(STRIP_MINUTES);
    expect(cells[0].start).toBe(at(0));
    expect(cells.at(-1)?.start).toBe(at(59));
    expect(countCells(cells)).toEqual({ received: 0, missed: 0, unknown: 60, before: 0 });
  });

  it("marks the minutes of observed heartbeats received, and the minutes nobody watched unknown", () => {
    // An online worker watched from minute 50: every 10 s read names a heartbeat at most 15 s old.
    const observations = [];
    for (let s = 50 * 60; s <= 59 * 60 + 50; s += 10) {
      observations.push({ at: T0 + s * 1000, last: T0 + Math.floor(s / 15) * 15_000 });
    }
    const cells = heartbeatCells(observations, at(-120), at(59, 55));
    expect(cells.slice(50).every((cell) => cell.state === "received")).toBe(true);
    expect(cells.slice(0, 50).every((cell) => cell.state === "unknown")).toBe(true);
  });

  it("marks a silence longer than a minute missed, from its first observation on", () => {
    // Opened at 59:00; the last heartbeat was at 54:20, so minutes 55 to 58 had none, and 59 has none so far.
    const cells = heartbeatCells([{ at: at(59, 0) + 500, last: at(54, 20) }, { at: at(59, 30), last: at(54, 20) }], at(-120), at(59, 31));
    expect(cells[54].state).toBe("received");
    expect(cells.slice(55, 60).map((cell) => cell.state)).toEqual(["missed", "missed", "missed", "missed", "missed"]);
    expect(cells[53].state).toBe("unknown");
    expect(missedRuns(cells)).toEqual([{ start: at(55), minutes: 5 }]);
  });

  it("does not call the time between two heartbeats a miss", () => {
    const cells = heartbeatCells([{ at: at(59, 40), last: at(59, 2) }], at(-120), at(59, 41));
    expect(cells[59].state).toBe("received");
    expect(countCells(cells).missed).toBe(0);
    const later = heartbeatCells([{ at: at(59, 40), last: at(58, 50) }], at(-120), at(59, 41));
    expect(later[59].state).toBe("unknown"); // 50 s without one: late, not yet missed
  });

  it("knows nothing about the time after the latest read", () => {
    const cells = heartbeatCells([{ at: at(50, 5), last: at(40) }], at(-120), at(59, 0));
    expect(cells[49].state).toBe("missed");
    expect(cells[50].state).toBe("missed"); // up to the read at 50:05
    expect(cells.slice(51).every((cell) => cell.state === "unknown")).toBe(true);
  });

  it("marks minutes before the worker registered, and a worker that never sent a heartbeat missed since then", () => {
    const created = at(57, 10);
    const cells = heartbeatCells([{ at: at(59, 40), last: null }], created, at(59, 41));
    expect(cells[56].state).toBe("before");
    expect(cells.slice(57).map((cell) => cell.state)).toEqual(["missed", "missed", "missed"]);
  });

  it("lets a heartbeat that arrived after a gap win its minute", () => {
    const cells = heartbeatCells(
      [
        { at: at(58, 30), last: at(57, 10) },
        { at: at(58, 40), last: at(58, 35) },
      ],
      at(-120),
      at(59, 1),
    );
    expect(cells[58].state).toBe("received");
    expect(cells[57].state).toBe("received");
    expect(cells[59].state).toBe("unknown");
  });
});

describe("the observations of this tab", () => {
  beforeEach(() => resetHeartbeats());

  it("records each answer once, keeps the order, and tells listeners", () => {
    const before = heartbeatsVersion();
    recordHeartbeats([{ id: 7, last_heartbeat_at: "2026-10-05T07:00:00Z" }], at(1));
    recordHeartbeats([{ id: 7, last_heartbeat_at: "2026-10-05T07:00:00Z" }], at(1)); // the same answer again
    recordHeartbeats([{ id: 7, last_heartbeat_at: null }], at(2));
    expect(observationsOf(7)).toEqual([
      { at: at(1), last: T0 },
      { at: at(2), last: null },
    ]);
    expect(heartbeatsVersion()).toBe(before + 2);
  });

  it("keeps them in sessionStorage and forgets what is older than the strip", () => {
    recordHeartbeats([{ id: 3, last_heartbeat_at: null }], at(0));
    recordHeartbeats([{ id: 3, last_heartbeat_at: null }], at(90));
    expect(observationsOf(3)).toEqual([{ at: at(90), last: null }]);
    expect(JSON.parse(window.sessionStorage.getItem("evo-hub:worker-heartbeats") ?? "{}")).toEqual({
      "3": [{ at: at(90), last: null }],
    });
  });
});
