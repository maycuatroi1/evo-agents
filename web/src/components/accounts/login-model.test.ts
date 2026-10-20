import { describe, expect, it } from "vitest";

import {
  defaultExpiryDay,
  emptyLoginForm,
  expiresAtOf,
  type LoginForm,
  loginBody,
  loginCommand,
  loginProblems,
  type NameMatch,
  nameMatch,
  renewForm,
  shellWord,
  tokenProblem,
} from "./login-model";
import type { Account } from "./queries";

const NOW = Date.parse("2026-10-14T09:30:00Z");
const TOKEN = "sk-ant-oat01-unit-token";

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  auth: "token",
  projects: ["demo", "docs"],
  workers: ["mac-mini"],
  weight: 2,
  priority: 1,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: null,
  state: "ready",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
};

const form = (patch: Partial<LoginForm> = {}): LoginForm => ({ ...emptyLoginForm(NOW), name: "claude-work", projects: ["demo"], ...patch });

describe("the day a new token ends", () => {
  it("is a year from today less a day, in UTC, as hub account login sets it", () => {
    expect(defaultExpiryDay(NOW)).toBe("2027-10-13");
    expect(defaultExpiryDay(Date.parse("2026-12-31T23:59:00Z"))).toBe("2027-12-30");
    // A leap day a year on is no day: the 1st of March less a day.
    expect(defaultExpiryDay(Date.parse("2028-02-29T12:00:00Z"))).toBe("2029-02-28");
    expect(emptyLoginForm(NOW)).toEqual({ name: "", projects: [], workers: [], expires: "2027-10-13" });
  });

  it("is sent as 00:00 UTC of the day, and a text that is no day is refused", () => {
    expect(expiresAtOf("2027-10-13")).toBe("2027-10-13T00:00:00.000Z");
    expect(expiresAtOf("2027-02-30")).toBeUndefined();
    expect(expiresAtOf("13/10/2027")).toBeUndefined();
  });

  it("starts a renewal from the account's name and bindings, and a new year", () => {
    expect(renewForm(ACCOUNT, NOW)).toEqual({ name: "claude-a", projects: ["demo", "docs"], workers: ["mac-mini"], expires: "2027-10-13" });
  });
});

describe("a token", () => {
  it("is refused empty, as a Console API key, with spaces inside, or too long", () => {
    expect(tokenProblem("   ")).toBe("tokenRequired");
    expect(tokenProblem("sk-ant-api03-abc")).toBe("tokenApiKey");
    expect(tokenProblem("sk-ant-oat01 abc")).toBe("tokenSpaces");
    expect(tokenProblem("sk-ant-oat01\u0000abc")).toBe("tokenSpaces");
    expect(tokenProblem("x".repeat(16_385))).toBe("tokenTooLong");
    expect(tokenProblem(` ${TOKEN}\n`)).toBeNull();
  });
});

describe("a name typed in the Add dialog", () => {
  const other: Account = { ...ACCOUNT, name: "openai", env_var: "OPENAI_API_KEY" };
  const git = { name: "gitlab-docs", kind: "git" as const, env_var: null };

  it("is free, an account of yours to renew, or another secret of yours", () => {
    expect(nameMatch("claude-new", [ACCOUNT], [git])).toEqual({ kind: "free" });
    expect(nameMatch("claude-a", [ACCOUNT], [])).toEqual({ kind: "account", account: ACCOUNT });
    expect(nameMatch("openai", [ACCOUNT, other], [])).toEqual({ kind: "other", secret: { name: "openai", kind: "env", env_var: "OPENAI_API_KEY" } });
    expect(nameMatch("gitlab-docs", [ACCOUNT], [git])).toEqual({ kind: "other", secret: git });
  });

  it("must be a secret's name, and not that of a secret which is no Claude account", () => {
    const check = (name: string, match: NameMatch = { kind: "free" }) =>
      loginProblems(form({ name }), TOKEN, { renewing: false, match, now: NOW }).name;
    expect(check("")).toBe("nameRequired");
    expect(check("Claude Work")).toBe("nameInvalid");
    expect(check("gitlab-docs", { kind: "other", secret: git })).toBe("nameOther");
    expect(check("claude-a", { kind: "account", account: ACCOUNT })).toBeUndefined();
    expect(check("claude-work")).toBeUndefined();
  });
});

describe("the form", () => {
  const free = { kind: "free" } as const;

  it("needs a project, an end ahead and a token; a renewal does not check its locked name", () => {
    expect(loginProblems(form({ projects: [], expires: "" }), "", { renewing: false, match: free, now: NOW })).toEqual({
      projects: "projectsRequired",
      expires: "expiresRequired",
      token: "tokenRequired",
    });
    expect(loginProblems(form({ expires: "2026-10-14" }), TOKEN, { renewing: false, match: free, now: NOW })).toEqual({ expires: "expiresPast" });
    expect(loginProblems(form({ name: "" }), TOKEN, { renewing: true, match: free, now: NOW })).toEqual({});
  });

  it("sends an env secret for CLAUDE_CODE_OAUTH_TOKEN with the token trimmed", () => {
    expect(loginBody(form({ workers: ["mac-mini"] }), ` ${TOKEN} `)).toEqual({
      kind: "env",
      env_var: "CLAUDE_CODE_OAUTH_TOKEN",
      projects: ["demo"],
      workers: ["mac-mini"],
      expires_at: "2027-10-13T00:00:00.000Z",
      value: TOKEN,
    });
  });
});

describe("the hub account login command", () => {
  it("names the account, each project and worker, and an end other than the default", () => {
    expect(loginCommand(form(), { renewing: false, now: NOW })).toEqual({
      text: "evo-agents hub account login claude-work --project demo",
      complete: true,
    });
    expect(loginCommand(form({ projects: ["demo", "docs"], workers: ["Mac mini"], expires: "2027-06-01" }), { renewing: false, now: NOW }).text).toBe(
      "evo-agents hub account login claude-work --project demo --project docs --worker 'Mac mini' --expires 2027-06-01",
    );
  });

  it("shows placeholders while a field it needs is missing", () => {
    expect(loginCommand(form({ name: "", projects: [] }), { renewing: false, now: NOW })).toEqual({
      text: "evo-agents hub account login <name> --project <project>",
      complete: false,
    });
  });

  it("names the account alone for a renewal, as the CLI keeps its projects and workers", () => {
    expect(loginCommand(renewForm(ACCOUNT, NOW), { renewing: true, now: NOW })).toEqual({
      text: "evo-agents hub account login claude-a",
      complete: true,
    });
  });

  it("quotes a word the shell would split or read", () => {
    expect(shellWord("lab-box.local")).toBe("lab-box.local");
    expect(shellWord("it's mine")).toBe(`'it'\\''s mine'`);
  });
});
