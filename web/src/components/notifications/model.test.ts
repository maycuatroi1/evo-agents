import { describe, expect, it } from "vitest";

import type { NotificationSettings, TierSetting } from "@/lib/notification-queries";

import {
  choiceOf,
  DEFAULT_CHOICE,
  defaultTier,
  overnight,
  sameZone,
  scheduleBody,
  scheduleChanged,
  scheduleDraft,
  scheduleErrors,
  tierBody,
  tierGroups,
} from "./model";

const tier = (kind: TierSetting["kind"], value: TierSetting["tier"], chosen = false): TierSetting => ({ kind, tier: value, chosen });

function settings(extra: Partial<NotificationSettings> = {}): NotificationSettings {
  return {
    notices: [{ kind: "run_outputs", on: true }],
    hide_details: false,
    tiers: [],
    quiet_hours: null,
    time_zone: "Asia/Ho_Chi_Minh",
    time_zone_chosen: false,
    digest_at: "07:30",
    ...extra,
  };
}

describe("tiers", () => {
  it("lists the kinds the hub pushes by default first, then the rest in the hub's order, a later kind included", () => {
    const tiers = [
      tier("decision", "interrupt"),
      tier("proposal", "brief"),
      tier("push_default_branch", "brief"),
      tier("run_failed", "interrupt"),
      tier("curator_brief", "off", true),
      tier("merge_paused", "interrupt"),
      tier("release", "interrupt"),
      tier("today", "brief"),
    ];
    const { first, rest } = tierGroups(tiers);
    expect(first.map((setting) => setting.kind)).toEqual(["decision", "run_failed", "release", "merge_paused", "proposal", "today"]);
    expect(rest.map((setting) => setting.kind)).toEqual(["push_default_branch", "curator_brief"]);
  });

  it("shows a kind never chosen as the hub's default, and puts a kind back to it with a null tier", () => {
    expect(choiceOf(tier("run_failed", "interrupt"))).toBe(DEFAULT_CHOICE);
    expect(choiceOf(tier("run_failed", "off", true))).toBe("off");
    expect(defaultTier("decision")).toBe("interrupt");
    expect(defaultTier("plan_finished")).toBe("brief");
    expect(defaultTier("a_kind_of_later")).toBe("brief");
    expect(tierBody("run_failed", "brief")).toEqual({ tiers: [{ kind: "run_failed", tier: "brief" }] });
    expect(tierBody("run_failed", DEFAULT_CHOICE)).toEqual({ tiers: [{ kind: "run_failed", tier: null }] });
  });
});

describe("the schedule", () => {
  it("starts from what the member chose, and from this browser's zone when they chose none", () => {
    expect(scheduleDraft(settings(), "Europe/Paris")).toEqual({ quiet: false, start: "22:00", end: "07:00", timeZone: "Europe/Paris", digestAt: "07:30" });
    expect(scheduleDraft(settings(), null).timeZone).toBe("Asia/Ho_Chi_Minh");
    const chosen = settings({ quiet_hours: { start: "23:00", end: "06:30" }, time_zone: "Asia/Tokyo", time_zone_chosen: true, digest_at: "08:00" });
    expect(scheduleDraft(chosen, "Europe/Paris")).toEqual({ quiet: true, start: "23:00", end: "06:30", timeZone: "Asia/Tokyo", digestAt: "08:00" });
  });

  it("reads a zone and its older name as one, so the hub's zone under the browser's name is no change", () => {
    expect(sameZone("Asia/Ho_Chi_Minh", "Asia/Saigon")).toBe(true);
    expect(sameZone("Asia/Tokyo", "Asia/Saigon")).toBe(false);
    expect(sameZone("Mars/Base", "Mars/Base")).toBe(true);
    expect(sameZone("Mars/Base", "Asia/Tokyo")).toBe(false);
    const held = settings();
    const hub = settings({ time_zone: "Asia/Ho_Chi_Minh" });
    expect(scheduleDraft(hub, "Asia/Saigon").timeZone).toBe("Asia/Ho_Chi_Minh");
    expect(scheduleChanged(hub, scheduleDraft(hub, "Asia/Saigon"))).toBe(false);
    expect(scheduleChanged(hub, { ...scheduleDraft(hub, null), timeZone: "Asia/Saigon" })).toBe(false);
    expect(scheduleChanged(held, { ...scheduleDraft(held, null), timeZone: "Asia/Tokyo" })).toBe(true);
  });

  it("counts a browser zone other than the hub's as a change to save, and nothing else when all is as held", () => {
    const held = settings();
    expect(scheduleChanged(held, scheduleDraft(held, "Asia/Ho_Chi_Minh"))).toBe(false);
    expect(scheduleChanged(held, scheduleDraft(held, "Europe/Paris"))).toBe(true);
    const draft = scheduleDraft(held, "Asia/Ho_Chi_Minh");
    expect(scheduleChanged(held, { ...draft, quiet: true })).toBe(true);
    expect(scheduleChanged(held, { ...draft, start: "21:00" })).toBe(false); // the times of quiet hours that are off
    expect(scheduleChanged(held, { ...draft, digestAt: "09:00" })).toBe(true);
    const quiet = settings({ quiet_hours: { start: "22:00", end: "07:00" } });
    expect(scheduleChanged(quiet, { ...scheduleDraft(quiet, null), end: "06:00" })).toBe(true);
    expect(scheduleChanged(quiet, { ...scheduleDraft(quiet, null), quiet: false })).toBe(true);
  });

  it("refuses what the hub would: a bad time, quiet hours that end when they start, a zone that is no IANA name", () => {
    const draft = scheduleDraft(settings(), "Asia/Ho_Chi_Minh");
    expect(scheduleErrors(draft)).toEqual({});
    expect(scheduleErrors({ ...draft, quiet: true, start: "", end: "24:00" })).toEqual({ start: "time", end: "time" });
    expect(scheduleErrors({ ...draft, quiet: true, start: "22:00", end: "22:00" })).toEqual({ end: "sameTime" });
    expect(scheduleErrors({ ...draft, quiet: false, start: "", end: "" })).toEqual({}); // off: the times are not sent
    expect(scheduleErrors({ ...draft, timeZone: "  " })).toEqual({ timeZone: "timeZone" });
    expect(scheduleErrors({ ...draft, timeZone: "Asia/Ho Chi Minh" })).toEqual({ timeZone: "timeZone" });
    expect(scheduleErrors({ ...draft, timeZone: `Etc/${"x".repeat(70)}` })).toEqual({ timeZone: "timeZone" });
    expect(scheduleErrors({ ...draft, digestAt: "7:30" })).toEqual({ digestAt: "time" });
  });

  it("saves quiet hours or none, the zone trimmed and the time of the summary", () => {
    const draft = { quiet: true, start: "22:30", end: "06:45", timeZone: " Europe/Paris ", digestAt: "08:15" };
    expect(scheduleBody(draft)).toEqual({ quiet_hours: { start: "22:30", end: "06:45" }, time_zone: "Europe/Paris", digest_at: "08:15" });
    expect(scheduleBody({ ...draft, quiet: false })).toEqual({ quiet_hours: null, time_zone: "Europe/Paris", digest_at: "08:15" });
  });

  it("knows quiet hours that run past midnight", () => {
    expect(overnight("22:00", "07:00")).toBe(true);
    expect(overnight("13:00", "14:00")).toBe(false);
  });
});
