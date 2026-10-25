import { describe, expect, it } from "vitest";

import {
  activeFilters,
  filterPlugins,
  filtersSearch,
  isUntrusted,
  NO_FILTERS,
  parseFilters,
  REPO_PATTERN,
  repoOf,
  requestableProjects,
  storeAction,
} from "./model";
import type { MarketPlugin } from "./queries";

function plugin(changes: Partial<MarketPlugin>): MarketPlugin {
  return {
    source: 1,
    market: "Northwind market",
    id: "lumen-notes",
    name: "Lumen notes",
    description: "Meeting notes for a project.",
    industries: ["education"],
    categories: ["documents"],
    repo: "lumen-labs/lumen-notes",
    publisher: { id: "lumen", name: "Lumen Labs", signer: "ed25519:abc", identity: "ed25519:abc" },
    tier: "isolated",
    price: { kind: "free" },
    status: "not_installed",
    ...changes,
  };
}

const NOTES = plugin({});
const LEDGER = plugin({
  id: "northwind-ledger",
  name: "Northwind ledger",
  description: "Invoices and time sheets.",
  industries: ["software-outsourcing"],
  categories: ["sales"],
  publisher: { id: "northwind", signer: "ed25519:def", identity: "ed25519:def" },
  price: { kind: "subscription", usd_per_hub_month: 49.5, seller: "northwind" },
  status: "needs_license",
});
const CRM = plugin({
  id: "crm",
  name: "CRM",
  description: "Leads and deals of a sales team.",
  publisher: { id: "evo", signer: "sigstore:maycuatroi1/evo-agents-core-plugins/.github/workflows/ci.yml", identity: "x" },
  tier: "core",
  status: "in_image",
  categories: ["sales"],
  industries: ["sales"],
});

describe("market filters", () => {
  it("reads the address and writes back only the filters in force", () => {
    const params = new URLSearchParams({ q: "  notes ", industry: "education", category: "x".repeat(65) });
    const filters = parseFilters(params);
    expect(filters).toEqual({ q: "notes", industry: "education", category: null });
    expect(filtersSearch(filters)).toBe("?q=notes&industry=education");
    expect(filtersSearch(NO_FILTERS)).toBe("");
    expect(activeFilters(filters)).toBe(2);
  });

  it("keeps the plugins of an industry, a category and every word of the search", () => {
    const all = [NOTES, LEDGER, CRM];
    expect(filterPlugins(all, NO_FILTERS)).toEqual(all);
    expect(filterPlugins(all, { ...NO_FILTERS, category: "sales" }).map((p) => p.id)).toEqual(["northwind-ledger", "crm"]);
    expect(filterPlugins(all, { ...NO_FILTERS, industry: "education" }).map((p) => p.id)).toEqual(["lumen-notes"]);
    expect(filterPlugins(all, { ...NO_FILTERS, q: "LUMEN meeting" }).map((p) => p.id)).toEqual(["lumen-notes"]);
    expect(filterPlugins(all, { ...NO_FILTERS, q: "northwind", category: "sales" }).map((p) => p.id)).toEqual([
      "northwind-ledger",
    ]);
  });
});

describe("market actions", () => {
  it("installs or upgrades a free isolated plugin only", () => {
    expect(storeAction(NOTES)).toBe("install");
    expect(storeAction({ ...NOTES, status: "upgrade" })).toBe("upgrade");
    expect(storeAction({ ...NOTES, status: "installed" })).toBeNull();
    expect(storeAction(LEDGER)).toBeNull();
    expect(storeAction(CRM)).toBeNull();
  });

  it("offers a request for the projects the member administers", () => {
    const grants = [
      { project: "lumen", role: "admin" },
      { project: "demo", role: "reader" },
      { project: "atlas", role: "admin" },
    ];
    expect(requestableProjects(grants)).toEqual(["atlas", "lumen"]);
  });

  it("tells an untrusted publisher from another conflict", () => {
    expect(isUntrusted("untrusted publisher: this hub does not trust ed25519:abc")).toBe(true);
    expect(isUntrusted("lumen-notes 1.0.0 is older than 1.1.0")).toBe(false);
    expect(isUntrusted(null)).toBe(false);
  });
});

describe("market source repo", () => {
  it("reads OWNER/REPO from what an admin pastes", () => {
    expect(repoOf(" https://github.com/northwind/market.git ")).toBe("northwind/market");
    expect(repoOf("github.com/northwind/market/tree/main")).toBe("northwind/market");
    expect(repoOf("northwind/market")).toBe("northwind/market");
    expect(REPO_PATTERN.test("northwind/market")).toBe(true);
    expect(REPO_PATTERN.test("northwind")).toBe(false);
    expect(REPO_PATTERN.test("north wind/market")).toBe(false);
  });
});
