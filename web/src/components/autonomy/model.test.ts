import { describe, expect, it } from "vitest";

import { draftOf, figureCounts, isChanged, levelAboveCeiling, loweredClasses, modeAboveCeiling, prefBody, presetMode } from "./model";
import type { AutonomySettings, SettingsClass } from "./queries";

const klass = (extra: Partial<SettingsClass> = {}): SettingsClass => ({
  name: "deploy",
  mode: "auto",
  source: "instance",
  detail: "",
  modes: ["deny", "ask", "notify", "auto"],
  ceiling: "auto",
  ceiling_source: "instance",
  pref: null,
  lowered: false,
  figures: null,
  ...extra,
});

const settings = (extra: Partial<AutonomySettings> = {}): AutonomySettings => ({
  principal: "ana",
  level: "A2",
  level_source: "unit:lab",
  revision: 3,
  pref_level: "A4",
  pref_classes: { K10: "deny" },
  ceiling: "A2",
  ceiling_source: "unit:lab",
  ceiling_detail: "Lab",
  classes: {
    K6: klass({ mode: "ask", ceiling: "ask", pref: "auto", lowered: true, figures: { refused: 2, asked: 0, decisions: 1, merged: null, reverted: null, rolled_back: 0 } }),
    K1: klass(),
  },
  stop: null,
  days: 30,
  ...extra,
});

describe("autonomy settings model", () => {
  it("reads the presets of autonomy_levels", () => {
    expect(presetMode("A0", "K1")).toBe("auto");
    expect(presetMode("A1", "K5")).toBe("ask");
    expect(presetMode("A3", "K12")).toBe("deny");
    expect(presetMode("A4", "K12")).toBe("ask");
  });

  it("starts the draft from the preference, A5 without one, and sees a change", () => {
    const held = settings();
    expect(draftOf(held)).toEqual({ level: "A4", classes: { K10: "deny" } });
    expect(draftOf(settings({ pref_level: null, pref_classes: {} })).level).toBe("A5");
    expect(isChanged(held, draftOf(held))).toBe(false);
    expect(isChanged(held, { level: "A3", classes: { K10: "deny" } })).toBe(true);
    expect(isChanged(held, { level: "A4", classes: { K10: "" } })).toBe(true);
  });

  it("resets the overrides only when the draft drops one", () => {
    const held = settings();
    expect(prefBody(held, { level: "A3", classes: { K10: "deny", K6: "ask" } })).toEqual({ level: "A3", classes: { K10: "deny", K6: "ask" }, reset: false });
    expect(prefBody(held, { level: "A3", classes: { K10: "" } })).toEqual({ level: "A3", classes: {}, reset: true });
  });

  it("puts levels and modes above the ceiling out of reach, and flags lowered classes", () => {
    const held = settings();
    expect(levelAboveCeiling(held, "A2")).toBe(false);
    expect(levelAboveCeiling(held, "A3")).toBe(true);
    expect(modeAboveCeiling(held, "K6", "ask")).toBe(false);
    expect(modeAboveCeiling(held, "K6", "notify")).toBe(true);
    expect(modeAboveCeiling(held, "K1", "auto")).toBe(false);
    expect(loweredClasses(held)).toEqual(["K6"]);
  });

  it("keeps the nonzero figures, none for a class without figures", () => {
    const held = settings();
    expect(figureCounts(held, "K6")).toEqual({ refused: 2, decisions: 1 });
    expect(figureCounts(held, "K1")).toBeNull();
  });
});
