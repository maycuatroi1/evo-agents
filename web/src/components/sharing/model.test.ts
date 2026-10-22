import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  type CredentialShare,
  emptyShareForm,
  endsSoon,
  isSubscription,
  liveShares,
  type ShareForm,
  shareBody,
  shareProblems,
  type ShareTarget,
  TERMS_WARNING,
} from "./model";

const NOW = Date.parse("2026-10-22T12:00:00Z");
const TOKEN: ShareTarget = { name: "claude-main", projects: ["beta", "alpha"], subscription: true };
const KEY: ShareTarget = { name: "search-key", projects: ["alpha"], subscription: false };

const form = (target: ShareTarget, patch: Partial<ShareForm> = {}): ShareForm => ({ ...emptyShareForm(target), ...patch });

function share(patch: Partial<CredentialShare> = {}): CredentialShare {
  return {
    id: 1,
    secret: "search-key",
    credential: "env",
    grantee_kind: "user",
    grantee: "lan-tran",
    projects: ["alpha"],
    level: null,
    budget_usd: null,
    usage_ceiling_pct: null,
    max_running: null,
    expires_at: null,
    terms_ack_at: null,
    created_by: "minh-le",
    created_at: "2026-10-01T00:00:00Z",
    revoked_at: null,
    revoked_by: null,
    ...patch,
  };
}

describe("TERMS_WARNING", () => {
  it("is the hub's text word for word", () => {
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
    const source = readFileSync(path.join(root, "evo_agents/hub/credential_shares.py"), "utf8");
    const block = /^TERMS_WARNING = \(\n((?:\s+".*"\n)+)\)/m.exec(source);
    expect(block).not.toBeNull();
    const text = [...(block?.[1] ?? "").matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1]).join("");
    expect(TERMS_WARNING).toBe(text);
  });
});

describe("isSubscription", () => {
  it("is a personal Claude token alone", () => {
    expect(isSubscription("env", "CLAUDE_CODE_OAUTH_TOKEN")).toBe(true);
    expect(isSubscription("env", "ANTHROPIC_API_KEY")).toBe(false);
    expect(isSubscription("git", null)).toBe(false);
  });
});

describe("shareProblems", () => {
  it("starts with every project of the target, sorted, and asks for a member", () => {
    expect(emptyShareForm(TOKEN).projects).toEqual(["alpha", "beta"]);
    expect(shareProblems(form(KEY), KEY, NOW)).toEqual({ login: "required" });
  });

  it("asks a subscription token for the acknowledgement, and nothing else for an API key", () => {
    expect(shareProblems(form(TOKEN, { login: "lan-tran" }), TOKEN, NOW)).toEqual({ termsAck: "ack" });
    expect(shareProblems(form(TOKEN, { login: "lan-tran", termsAck: true }), TOKEN, NOW)).toEqual({});
    expect(shareProblems(form(KEY, { login: "lan-tran" }), KEY, NOW)).toEqual({});
  });

  it("checks each limit against the hub's bounds", () => {
    const bad = form(KEY, {
      granteeKind: "unit",
      unit: "Team A",
      projects: [],
      budget: "0",
      ceiling: "101",
      maxRunning: "1.5",
      expires: "2026-10-22",
    });
    expect(shareProblems(bad, KEY, NOW)).toEqual({
      unit: "pattern",
      projects: "required",
      budget: "range",
      ceiling: "range",
      maxRunning: "whole",
      expires: "past",
    });
    expect(shareProblems(form(KEY, { granteeKind: "all", budget: "12.345" }), KEY, NOW)).toEqual({ budget: "pattern" });
  });
});

describe("shareBody", () => {
  it("sends only the limits set, and the acknowledgement of a subscription", () => {
    const filled = form(TOKEN, { login: " lan-tran ", budget: "25.5", ceiling: "80", expires: "2026-11-01", termsAck: true });
    expect(shareBody(filled, TOKEN)).toEqual({
      grantee_kind: "user",
      login: "lan-tran",
      projects: ["alpha", "beta"],
      budget_usd: 25.5,
      usage_ceiling_pct: 80,
      expires_at: "2026-11-01T00:00:00Z",
      terms_ack: true,
    });
    expect(shareBody(form(KEY, { granteeKind: "unit", unit: "team-a", level: "A2", maxRunning: "3" }), KEY)).toEqual({
      grantee_kind: "unit",
      unit: "team-a",
      projects: ["alpha"],
      level: "A2",
      max_running: 3,
      terms_ack: false,
    });
  });
});

describe("endsSoon and liveShares", () => {
  it("says a share ends soon within a week, never after it ended", () => {
    expect(endsSoon(share({ expires_at: "2026-10-25T00:00:00Z" }), NOW)).toBe(true);
    expect(endsSoon(share({ expires_at: "2026-11-25T00:00:00Z" }), NOW)).toBe(false);
    expect(endsSoon(share({ expires_at: "2026-10-01T00:00:00Z" }), NOW)).toBe(false);
    expect(endsSoon(share(), NOW)).toBe(false);
  });

  it("lists the live shares of every item, newest first", () => {
    const items = [{ shares: [share({ id: 3 }), share({ id: 4, revoked_at: "2026-10-02T00:00:00Z" })] }, { shares: [share({ id: 7 })] }];
    expect(liveShares(items).map((item) => item.id)).toEqual([7, 3]);
  });
});
