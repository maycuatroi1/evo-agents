import { describe, expect, it } from "vitest";

import { resetWhen, sharePercent, UNNAMED, usageStatus, usageWindows, windowLabel } from "./usage-model";

const NOW = Date.parse("2026-10-14T04:09:00Z");
const IN_3H = "2026-10-14T07:00:00Z";
const NEXT_TUESDAY = "2026-10-20T00:00:00Z";
const PAST = "2026-10-14T03:00:00Z";

describe("an account's usage windows", () => {
  it("lists each window with its share in whole percent, sessions first, then the week and its models", () => {
    const usage = {
      status: "allowed" as const,
      rate_limit_type: "five_hour",
      utilization: 0.35,
      windows: {
        seven_day_sonnet: { utilization: 0.1, resets_at: NEXT_TUESDAY },
        seven_day: { utilization: 0.224, resets_at: NEXT_TUESDAY },
        five_hour: { utilization: 0.35, resets_at: IN_3H },
        overage: { utilization: 0 },
        seven_day_opus: { utilization: 0, resets_at: NEXT_TUESDAY },
      },
    };
    const windows = usageWindows(usage, NOW);
    expect(windows.map((window) => [window.name, window.percent, window.tone])).toEqual([
      ["five_hour", 35, "normal"],
      ["seven_day", 22, "normal"],
      ["seven_day_opus", 0, "normal"],
      ["seven_day_sonnet", 10, "normal"],
      ["overage", 0, "normal"],
    ]);
    expect(windows[0]).toMatchObject({ resetsAt: IN_3H, reset: false, limited: false });
  });

  it("warns from 80% and is in danger at 100% or at the limit the CLI rejected, a share above 1 being the limit", () => {
    const usage = {
      status: "rejected" as const,
      rate_limit_type: "seven_day",
      resets_at: NEXT_TUESDAY,
      windows: { five_hour: { utilization: 0.8 }, seven_day: { utilization: 0.97 }, seven_day_opus: { utilization: 1.4 } },
    };
    expect(usageWindows(usage, NOW).map((window) => [window.name, window.percent, window.tone, window.limited])).toEqual([
      ["five_hour", 80, "warning", false],
      ["seven_day", 97, "danger", true],
      ["seven_day_opus", 100, "danger", false],
    ]);
    expect(sharePercent(0.799)).toBe(80);
    expect(sharePercent(-0.2)).toBe(0);
    expect(sharePercent(85)).toBe(100);
    expect(sharePercent(null)).toBeNull();
  });

  it("reads a window whose reset has passed as reset, at 0%, and the status it reported as over", () => {
    const usage = {
      status: "rejected" as const,
      rate_limit_type: "five_hour",
      windows: { five_hour: { utilization: 1, resets_at: PAST }, seven_day: { utilization: 0.5, resets_at: NEXT_TUESDAY } },
    };
    const [session, week] = usageWindows(usage, NOW);
    expect(session).toMatchObject({ name: "five_hour", percent: 0, reset: true, limited: false, tone: "normal" });
    expect(week).toMatchObject({ percent: 50, reset: false });
    expect(usageStatus(usage, NOW)).toBe("allowed");
    expect(usageStatus(usage, Date.parse(PAST) - 1)).toBe("rejected");
  });

  it("takes the limit a report names from its own fields when no window holds it, and leaves out what says nothing", () => {
    expect(usageWindows({ status: "allowed_warning", rate_limit_type: "five_hour", utilization: 0.9, resets_at: IN_3H }, NOW)).toEqual([
      { name: "five_hour", percent: 90, resetsAt: IN_3H, reset: false, limited: false, tone: "warning" },
    ]);
    const unnamed = usageWindows({ status: "rejected", resets_at: IN_3H }, NOW);
    expect(unnamed).toEqual([{ name: UNNAMED, percent: 100, resetsAt: IN_3H, reset: false, limited: true, tone: "danger" }]);
    expect(usageWindows({ status: "allowed" }, NOW)).toEqual([]);
    expect(usageWindows({ status: "allowed", windows: { five_hour: {} } }, NOW)).toEqual([]);
    expect(usageWindows({ status: "allowed", windows: { five_hour: { resets_at: IN_3H } } }, NOW)[0].percent).toBeNull();
    expect(usageWindows(null, NOW)).toEqual([]);
  });
});

describe("the name of a window", () => {
  it("reads the session, the week and their models, and any other name as its words", () => {
    expect(windowLabel("five_hour")).toEqual({ key: "five_hour" });
    expect(windowLabel("seven_day")).toEqual({ key: "seven_day" });
    expect(windowLabel("seven_day_opus")).toEqual({ key: "weekly", model: "Opus" });
    expect(windowLabel("seven_day_claude_design")).toEqual({ key: "weekly", model: "Claude Design" });
    expect(windowLabel("five_hour_sonnet")).toEqual({ key: "session", model: "Sonnet" });
    expect(windowLabel("seven_day_")).toEqual({ key: "other", text: "Seven day" });
    expect(windowLabel("overage.monthly")).toEqual({ key: "other", text: "Overage monthly" });
    expect(windowLabel(UNNAMED)).toEqual({ key: "unnamed" });
  });
});

describe("when a window resets", () => {
  it("says hours and minutes within a day, the weekday within a week, the date past that, and past once it is", () => {
    expect(resetWhen("2026-10-14T07:00:00Z", NOW)).toEqual({ kind: "in", hours: 2, minutes: 51 });
    expect(resetWhen("2026-10-14T04:09:20Z", NOW)).toEqual({ kind: "in", hours: 0, minutes: 1 });
    expect(resetWhen("2026-10-15T04:09:00Z", NOW)).toEqual({ kind: "in", hours: 24, minutes: 0 });
    expect(resetWhen(NEXT_TUESDAY, NOW).kind).toBe("weekday");
    expect(resetWhen("2026-10-21T04:09:00Z", NOW).kind).toBe("date");
    expect(resetWhen(PAST, NOW).kind).toBe("past");
  });
});
