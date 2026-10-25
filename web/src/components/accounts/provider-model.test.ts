import { describe, expect, it } from "vitest";

import { inkOn, mergeRunnable, providerCards } from "./provider-cards";
import {
  addModel,
  catalogMatches,
  editProviderForm,
  filterCounts,
  freeName,
  hostOf,
  keyProblem,
  newProviderForm,
  probeBody,
  providerChangeBody,
  providerProblems,
  providerWriteBody,
  takenNames,
} from "./provider-model";
import type { CatalogProvider, ProviderAccount } from "./provider-queries";
import type { Account } from "./queries";

const entry = (id: string, group: string, extra: Partial<CatalogProvider> = {}): CatalogProvider => ({
  id,
  alias: id.slice(0, 2),
  name: id.toUpperCase(),
  group,
  color: "#10A37F",
  text_icon: id.slice(0, 2).toUpperCase(),
  website: "",
  key_hint: "",
  base_url: `https://api.${id}.example/v1`,
  api_format: "openai",
  default_model: `${id}-large`,
  key_required: true,
  free: false,
  via: "gateway",
  ...extra,
});

const CATALOG = [
  entry("claude", "login", { name: "Claude", via: "login", base_url: "" }),
  entry("openai", "apikey", { name: "OpenAI" }),
  entry("openrouter", "aggregator", { name: "OpenRouter", alias: "or" }),
  entry("glm", "regional", { name: "GLM (Z.ai)" }),
  entry("ollama", "local", { name: "Ollama", base_url: "http://localhost:11434/v1" }),
  entry("custom-openai", "custom", { name: "Custom OpenAI", base_url: "", default_model: "" }),
];

const ACCOUNT: ProviderAccount = {
  name: "glm",
  provider: "glm",
  provider_name: "GLM (Z.ai)",
  alias: "glm",
  base_url: "https://api.z.ai/api/coding/paas/v4",
  api_format: "openai",
  models: ["glm-5.3-flash"],
  projects: ["demo"],
  workers: [],
  weight: 1,
  priority: 0,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: null,
  state: "ready",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

const TODAY = "2026-10-25";
const KEY = "e2e-good-0123456789";

describe("the catalog", () => {
  it("filters by group, Popular holding the login, key and free providers", () => {
    expect(catalogMatches(CATALOG, "", "popular").map((item) => item.id)).toEqual(["claude", "openai"]);
    expect(catalogMatches(CATALOG, "", "gateways").map((item) => item.id)).toEqual(["openrouter"]);
    expect(catalogMatches(CATALOG, "", "all")).toHaveLength(CATALOG.length);
    expect(filterCounts(CATALOG)).toMatchObject({ all: 6, popular: 2, gateways: 1, regional: 1, local: 1, custom: 1, inference: 0 });
  });

  it("searches the name, the id and the alias, every word, in any case", () => {
    expect(catalogMatches(CATALOG, "z.AI", "all").map((item) => item.id)).toEqual(["glm"]);
    expect(catalogMatches(CATALOG, "or", "gateways").map((item) => item.id)).toEqual(["openrouter"]);
    expect(catalogMatches(CATALOG, "custom openai", "all").map((item) => item.id)).toEqual(["custom-openai"]);
    expect(catalogMatches(CATALOG, "nothing", "all")).toEqual([]);
  });
});

describe("a new provider account", () => {
  it("takes the catalog's URL, format and model, and a name free among the visitor's", () => {
    const form = newProviderForm(CATALOG[3], new Set(["glm", "glm-2"]));
    expect(form).toMatchObject({ name: "glm-3", baseUrl: "https://api.glm.example/v1", format: "openai", models: ["glm-large"] });
    expect(freeName("Custom OpenAI!", new Set())).toBe("custom-openai-");
    expect(freeName("--", new Set())).toBe("provider");
  });

  it("names every field the hub would refuse, before anything is sent", () => {
    const form = { ...newProviderForm(CATALOG[5], new Set()), name: "Bad Name" };
    expect(providerProblems(form, { key: "", taken: new Set(), editing: false, today: TODAY })).toEqual({
      name: "nameInvalid",
      baseUrl: "urlRequired",
      key: "keyRequired",
      projects: "projectsRequired",
    });
    const local = { ...newProviderForm(CATALOG[4], new Set()), projects: ["demo"] };
    expect(providerProblems(local, { key: KEY, taken: new Set(), editing: false, today: TODAY })).toEqual({ baseUrl: "urlHttps" });
    const taken = { ...newProviderForm(CATALOG[3], new Set()), projects: ["demo"], expires: TODAY };
    expect(providerProblems(taken, { key: KEY, taken: new Set(["glm"]), editing: false, today: TODAY })).toEqual({
      name: "nameTaken",
      expires: "expiresPast",
    });
    expect(providerProblems({ ...taken, source: "secret" }, { key: "", taken: new Set(), editing: false, today: "2026-10-01" })).toEqual({
      secret: "secretRequired",
    });
  });

  it("takes a key of one line of at most 16384 bytes", () => {
    expect(keyProblem("  ")).toBe("keyRequired");
    expect(keyProblem("a\nb")).toBe("keyLine");
    expect(keyProblem("x".repeat(16385))).toBe("keyTooLong");
    expect(keyProblem(` ${KEY} `)).toBeNull();
  });

  it("sends the key pasted, or the env secret that holds it, and the end of the day picked", () => {
    const form = { ...newProviderForm(CATALOG[3], new Set()), projects: ["demo"], expires: "2027-01-02" };
    expect(providerWriteBody(form, "glm", ` ${KEY} `)).toEqual({
      provider: "glm",
      base_url: "https://api.glm.example/v1",
      api_format: "openai",
      value: KEY,
      models: ["glm-large"],
      projects: ["demo"],
      workers: [],
      expires_at: "2027-01-02T00:00:00.000Z",
    });
    const fromSecret = providerWriteBody({ ...form, source: "secret", secret: "glm-key", expires: "" }, "glm", "ignored");
    expect(fromSecret).toMatchObject({ from_secret: "glm-key" });
    expect(fromSecret).not.toHaveProperty("value");
    expect(fromSecret).not.toHaveProperty("expires_at");
    expect(probeBody(form, "glm", KEY, "glm-large")).toEqual({
      provider: "glm",
      base_url: "https://api.glm.example/v1",
      api_format: "openai",
      value: KEY,
      model: "glm-large",
    });
  });

  it("refuses the names a secret or a provider account of the visitor holds", () => {
    expect(takenNames([{ name: "gh", kind: "env", env_var: "GH_TOKEN" }], [ACCOUNT])).toEqual(new Set(["gh", "glm"]));
  });
});

describe("Edit of a provider account", () => {
  it("asks for no key and sends only what changed", () => {
    const form = editProviderForm(ACCOUNT);
    expect(providerProblems(form, { key: "", taken: new Set(["glm"]), editing: true, today: TODAY })).toEqual({});
    expect(providerChangeBody(form, ACCOUNT)).toBeNull();
    const changed = { ...form, models: [...form.models, "glm-5.3"], workers: ["laptop"], expires: "2027-03-01" };
    expect(providerChangeBody(changed, ACCOUNT)).toEqual({
      models: ["glm-5.3-flash", "glm-5.3"],
      workers: ["laptop"],
      expires_at: "2027-03-01T00:00:00.000Z",
    });
    const dropped = providerChangeBody(editProviderForm({ ...ACCOUNT, expires_at: "2027-03-01T00:00:00Z" }), ACCOUNT);
    expect(dropped).toEqual({ expires_at: "2027-03-01T00:00:00.000Z" });
    expect(providerChangeBody(form, { ...ACCOUNT, expires_at: "2027-03-01T00:00:00Z" })).toEqual({ expires_at: null });
  });
});

describe("the models of a form", () => {
  it("adds a name once, and refuses one the hub would", () => {
    expect(addModel(["a"], " b ")).toEqual({ models: ["a", "b"], problem: null });
    expect(addModel(["a"], "a")).toEqual({ models: ["a"], problem: null });
    expect(addModel(["a"], "has space")).toEqual({ models: ["a"], problem: "modelInvalid" });
    expect(addModel(Array.from({ length: 500 }, (_, index) => `m${index}`), "new").problem).toBe("modelsTooMany");
  });
});

describe("the host of a URL", () => {
  it("is null for a text that is no URL", () => {
    expect(hostOf("https://api.z.ai/api/v4")).toBe("api.z.ai");
    expect(hostOf("not a url")).toBeNull();
  });
});

const account = (name: string, extra: Partial<Account>): Account => ({
  name,
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  provider: "claude",
  category: "model",
  auth: "token",
  projects: ["demo"],
  workers: [],
  pools: [],
  weight: 1,
  priority: 0,
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
  shares: [],
  cost_by_principal: [],
  ...extra,
});

describe("the provider cards", () => {
  it("put Claude first, then each provider by name, with the models of its provider accounts", () => {
    const gateway = { provider: "glm", prefix: "glm", base_url_host: "api.z.ai", api_format: "openai", models: 1, status: "ok", reason: null } as const;
    const cards = providerCards(
      [
        account("glm", { env_var: "gateway:glm", provider: "other", gateway }),
        account("openai-key", { env_var: "OPENAI_API_KEY", provider: "openai" }),
        account("claude-a", {}),
        account("gitlab", { env_var: "GITLAB_TOKEN", provider: "gitlab", category: "service" }),
      ],
      [ACCOUNT],
      CATALOG,
    );
    expect(cards.map((card) => [card.id, card.accounts.map((item) => item.name)])).toEqual([
      ["claude", ["claude-a"]],
      ["glm", ["glm"]],
      ["openai", ["openai-key"]],
    ]);
    expect(cards[1].models).toEqual(["glm-5.3-flash"]);
    expect(cards[1].keys.get("glm")?.base_url).toBe(ACCOUNT.base_url);
  });
});

describe("the Models tab", () => {
  it("merges the models of every project by name", () => {
    const one = { model: "glm/glm-5.3-flash", prefix: "glm", runtimes: ["opencode"], accounts: ["glm"] };
    const rows = mergeRunnable([
      { project: "demo", models: [one] },
      { project: "web", models: [{ ...one, runtimes: ["opencode", "claude-code"] }] },
    ]);
    expect(rows).toEqual([{ ...one, runtimes: ["opencode", "claude-code"], projects: ["demo", "web"] }]);
  });
});

describe("the ink on a catalog colour", () => {
  it("is white on a dark colour and dark on a light one", () => {
    expect(inkOn("#10A37F")).toBe("dark");
    expect(inkOn("#5B21B6")).toBe("light");
    expect(inkOn("#000000")).toBe("light");
    expect(inkOn("#F5D90A")).toBe("dark");
    // A mid colour takes the higher contrast: black at 4.7:1 on this indigo, where white has 4.47:1.
    expect(inkOn("#6366F1")).toBe("dark");
    expect(inkOn("teal")).toBe("light");
  });
});
