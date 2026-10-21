import { describe, expect, it } from "vitest";

import { brandingCss, type Features, featureOn } from "./hub-settings";

const FEATURES: Features = {
  features: [
    { id: "chats", layers: ["instance"], enabled: false },
    { id: "curator", layers: ["instance", "project"], enabled: true },
    { id: "kg", layers: ["instance", "project"], enabled: false },
    { id: "monitor", layers: ["instance"], enabled: true },
  ],
  projects: [
    { project: "alpha", values: { kg: true, curator: false } },
    { project: "beta", values: { chats: true } },
  ],
};

describe("featureOn", () => {
  it("is on while the features are unknown and for an item of no feature, as a hub with nothing set", () => {
    expect(featureOn(undefined, "kg", "alpha")).toBe(true);
    expect(featureOn(FEATURES, undefined, "alpha")).toBe(true);
  });

  it("takes a project's layer over the hub's for a feature a project may set", () => {
    expect(featureOn(FEATURES, "kg")).toBe(false);
    expect(featureOn(FEATURES, "kg", "alpha")).toBe(true);
    expect(featureOn(FEATURES, "kg", "gamma")).toBe(false);
    expect(featureOn(FEATURES, "curator", "alpha")).toBe(false);
    expect(featureOn(FEATURES, "curator", "beta")).toBe(true);
  });

  it("reads the hub's layer alone for a feature of the whole hub", () => {
    expect(featureOn(FEATURES, "chats", "beta")).toBe(false);
    expect(featureOn(FEATURES, "monitor", null)).toBe(true);
  });
});

describe("brandingCss", () => {
  it("is empty without colours, so the page keeps the tokens of globals.css", () => {
    expect(brandingCss(undefined)).toBe("");
    expect(brandingCss({ light: {}, dark: {} })).toBe("");
  });

  it("writes one rule per theme over the tokens it sets, in lower case", () => {
    expect(brandingCss({ light: { brand: "#0F766E", "on-brand": "#ffffff" }, dark: { "brand-soft": "#0b2f2c" } })).toBe(
      "html:root:not(.dark){--brand:#0f766e;--on-brand:#ffffff}html.dark{--brand-soft:#0b2f2c}",
    );
  });

  it("leaves out anything but a #rrggbb colour of a token a hub may set", () => {
    const colors = { light: { brand: "red;}body{display:none", danger: "#000000", "brand-hover": "#12345" }, dark: {} };
    expect(brandingCss(colors)).toBe("");
  });
});
