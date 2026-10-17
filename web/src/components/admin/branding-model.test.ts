import { describe, expect, it } from "vitest";

import { brandingWrite } from "./branding-form";
import { checkColors, colorsOf, contrast, nameProblem, shownRatio, storedColors } from "./branding-model";
import { featuresWrite } from "./features-forms";
import type { SettingsLayer } from "./settings-data";

function layer(values: Record<string, unknown>, version = 3): SettingsLayer {
  return { kind: "branding", project: null, version, values, rolled_back_to: null, changed_by: "octo", changed_at: null };
}

describe("the contrast rules of the hub", () => {
  it("measures the defaults as web/DESIGN.md and evo_agents.hub.branding do", () => {
    expect(shownRatio(contrast("#2f4bd8", "#ffffff"))).toBe("6.73");
    expect(shownRatio(contrast("#8c9dff", "#111419"))).toBe("7.36");
    expect(shownRatio(contrast("#0a0c10", "#8c9dff"))).toBe("7.81");
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21);
    expect(checkColors({ light: {}, dark: {} }).every((result) => result.passes)).toBe(true);
  });

  it("names each pair a colour leaves under its ratio, the unset tokens at their defaults", () => {
    const results = checkColors({ light: { brand: "#9aa5ff" }, dark: {} });
    const failing = results.filter((result) => !result.passes).map((result) => `${result.theme}:${result.id}`);
    expect(failing).toEqual(["light:linkOnCard", "light:linkOnPage", "light:pill", "light:navIcon", "light:onBrand"]);
    expect(shownRatio(results[0].ratio)).toBe("2.28");
    // a value that is no colour yet is left out, so the default is measured
    expect(checkColors({ light: { brand: "#9aa5f" }, dark: {} }).every((result) => result.passes)).toBe(true);
  });

  it("rounds a ratio down, so a pair just under its least never reads as passing", () => {
    expect(shownRatio(4.4999)).toBe("4.49");
  });

  it("refuses a blank, long or untrimmed name", () => {
    expect(nameProblem("Acme Agents")).toBeNull();
    expect(nameProblem("   ")).toBe("required");
    expect(nameProblem("x".repeat(61))).toBe("invalid");
    expect(nameProblem(" Acme")).toBe("invalid");
    expect(nameProblem("Acme\nAgents")).toBe("invalid");
  });
});

describe("a save of the branding", () => {
  const held = layer({ name: "Acme", colors: { light: { brand: "#0F766E" } }, logo: { sha256: "a".repeat(64) } });

  it("sends nothing when nothing changed, colours compared in lower case", () => {
    expect(colorsOf(held.values)).toEqual({ light: { brand: "#0f766e" }, dark: {} });
    expect(brandingWrite(held, { name: "Acme", colors: { light: { brand: "#0f766e" }, dark: {} } })).toEqual({
      values: {},
      unset: [],
      if_version: 3,
    });
  });

  it("sets what changed and unsets what was emptied, at the layer's version", () => {
    const body = brandingWrite(held, { name: "", colors: { light: { brand: "#0f766e" }, dark: { brand: "#5EEAD4" } } });
    expect(body).toEqual({
      values: { colors: { light: { brand: "#0f766e" }, dark: { brand: "#5eead4" } } },
      unset: ["name"],
      if_version: 3,
    });
    expect(brandingWrite(held, { name: "Acme", colors: { light: { brand: "" }, dark: {} } }).unset).toEqual(["colors"]);
    expect(storedColors({ light: { brand: "" }, dark: { "on-brand": "#FFFFFF" } })).toEqual({ dark: { "on-brand": "#ffffff" } });
  });
});

describe("a save of a layer of features", () => {
  const ids = ["curator", "kg"] as const;
  const held = { ...layer({ kg: false }, 2), kind: "features" as const, project: "alpha" };

  it("turns features on or off and unsets those that follow the hub again", () => {
    expect(featuresWrite(held, ids, {})).toEqual({ values: {}, unset: [], if_version: 2 });
    expect(featuresWrite(held, ids, { kg: "off" })).toEqual({ values: {}, unset: [], if_version: 2 });
    expect(featuresWrite(held, ids, { kg: "hub", curator: "off" })).toEqual({
      values: { curator: false },
      unset: ["kg"],
      if_version: 2,
    });
    expect(featuresWrite(held, ids, { kg: "on" }).values).toEqual({ kg: true });
  });
});
