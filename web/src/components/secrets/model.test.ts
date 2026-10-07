import { describe, expect, it } from "vitest";

import {
  emptySecretForm,
  envVarProblem,
  expiresAt,
  formOf,
  isExpired,
  secretBody,
  type SecretForm,
  secretProblems,
  urlPrefixProblem,
} from "./model";
import type { Secret } from "./queries";

const NOW = Date.parse("2026-10-07T09:00:00Z");

const SECRET: Secret = {
  name: "gitlab-docs",
  kind: "git",
  env_var: null,
  url_prefix: "https://gitlab.example.org/group",
  username: "oauth2",
  projects: ["demo"],
  workers: ["mac-mini"],
  expires_at: "2027-09-15T00:00:00+00:00",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-02T00:00:00Z",
};

function form(patch: Partial<SecretForm>): SecretForm {
  return { ...emptySecretForm(), name: "claude-oauth", envVar: "CLAUDE_CODE_OAUTH_TOKEN", projects: ["demo"], value: "v", ...patch };
}

const checks = (patch: Partial<SecretForm>, taken: string[] = []) => secretProblems(form(patch), { adding: true, taken, now: NOW });

describe("secret form", () => {
  it("passes a complete env secret and sends only the fields of its kind", () => {
    expect(checks({})).toEqual({});
    expect(secretBody(form({ urlPrefix: "https://left.over", username: "x" }))).toEqual({
      kind: "env",
      env_var: "CLAUDE_CODE_OAUTH_TOKEN",
      projects: ["demo"],
      workers: [],
      expires_at: null,
      value: "v",
    });
  });

  it("checks the name, and refuses one of the visitor's secrets when adding", () => {
    expect(checks({ name: "" }).name).toBe("nameRequired");
    expect(checks({ name: "Claude" }).name).toBe("nameInvalid");
    expect(checks({ name: "-x" }).name).toBe("nameInvalid");
    expect(checks({ name: "a".repeat(65) }).name).toBe("nameInvalid");
    expect(checks({ name: "claude-oauth" }, ["claude-oauth"]).name).toBe("nameTaken");
    // A replace keeps its name: nothing to check.
    expect(secretProblems(form({ name: "" }), { adding: false, taken: [], now: NOW }).name).toBeUndefined();
  });

  it("refuses the variables the hub refuses", () => {
    expect(envVarProblem("")).toBe("envVarRequired");
    expect(envVarProblem("claude_token")).toBe("envVarInvalid");
    expect(envVarProblem("1TOKEN")).toBe("envVarInvalid");
    for (const name of ["PATH", "HOME", "SSH_AUTH_SOCK", "EVO_HUB_TOKEN", "GIT_ASKPASS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH"]) {
      expect(envVarProblem(name), name).toBe("envVarDenied");
    }
    expect(envVarProblem("CLAUDE_CODE_OAUTH_TOKEN")).toBeNull();
    expect(envVarProblem("_PRIVATE")).toBeNull();
  });

  it("takes an https prefix without a user, a password, a query or a fragment", () => {
    expect(urlPrefixProblem("")).toBe("urlPrefixRequired");
    expect(urlPrefixProblem("http://gitlab.example.org/group")).toBe("urlPrefixInvalid");
    expect(urlPrefixProblem("gitlab.example.org/group")).toBe("urlPrefixInvalid");
    expect(urlPrefixProblem("https://oauth2:secret@gitlab.example.org/group")).toBe("urlPrefixCredentials");
    expect(urlPrefixProblem("https://gitlab.example.org/group?x=1")).toBe("urlPrefixInvalid");
    expect(urlPrefixProblem("https://gitlab.example.org/group#top")).toBe("urlPrefixInvalid");
    expect(urlPrefixProblem("https://gitlab.example.org:99999/group")).toBe("urlPrefixInvalid");
    expect(urlPrefixProblem(" https://GitLab.Example.org:8443/group/ ")).toBeNull();
    expect(checks({ kind: "git", urlPrefix: "https://gitlab.example.org", username: "bad\tuser" }).username).toBe("usernameInvalid");
  });

  it("sends a git secret's prefix, and its username only when one is given", () => {
    const git = form({ kind: "git", urlPrefix: " https://gitlab.example.org/group ", username: "", envVar: "LEFT_OVER" });
    expect(secretProblems(git, { adding: true, taken: [], now: NOW })).toEqual({});
    expect(secretBody(git)).toEqual({
      kind: "git",
      url_prefix: "https://gitlab.example.org/group",
      projects: ["demo"],
      workers: [],
      expires_at: null,
      value: "v",
    });
    expect(secretBody({ ...git, username: "deploy" })).toMatchObject({ username: "deploy" });
  });

  it("needs a project, and a value the hub takes", () => {
    expect(checks({ projects: [] }).projects).toBe("projectsRequired");
    expect(checks({ value: "" }).value).toBe("valueRequired");
    expect(checks({ value: "é".repeat(8193) }).value).toBe("valueTooLong"); // 16386 bytes of UTF-8
    expect(checks({ value: "a".repeat(16384) }).value).toBeUndefined();
    expect(checks({ value: "a\u0000b" }).value).toBe("valueNul");
    expect(checks({ kind: "git", urlPrefix: "https://gitlab.example.org", value: "one\ntwo" }).value).toBe("valueLines");
    expect(checks({ value: "-----BEGIN-----\nline\n" }).value).toBeUndefined(); // an env value may span lines
  });

  it("ends a secret at 00:00 UTC of the day chosen, in the future", () => {
    expect(expiresAt("")).toBeNull();
    expect(expiresAt("2027-09-15")).toBe("2027-09-15T00:00:00.000Z");
    expect(expiresAt("2027-02-30")).toBeUndefined();
    expect(expiresAt("15/09/2027")).toBeUndefined();
    expect(checks({ expires: "2026-10-07" }).expires).toBe("expiresPast");
    expect(checks({ expires: "2026-10-08" }).expires).toBeUndefined();
    // A day left as the secret had it keeps the secret's own time.
    expect(expiresAt("2027-09-15", { ...SECRET, expires_at: "2027-09-15T13:30:00+00:00" })).toBe("2027-09-15T13:30:00+00:00");
    expect(expiresAt("2027-09-16", { ...SECRET, expires_at: "2027-09-15T13:30:00+00:00" })).toBe("2027-09-16T00:00:00.000Z");
  });

  it("fills a replace with everything the hub shows of the secret, but never a value", () => {
    expect(formOf(SECRET)).toEqual({
      name: "gitlab-docs",
      kind: "git",
      envVar: "",
      urlPrefix: "https://gitlab.example.org/group",
      username: "oauth2",
      projects: ["demo"],
      workers: ["mac-mini"],
      expires: "2027-09-15",
      value: "",
    });
    const body = secretBody({ ...formOf(SECRET), value: "new" }, SECRET);
    expect(body.expires_at).toBe(SECRET.expires_at);
    expect(body.value).toBe("new");
  });

  it("tells an expired secret", () => {
    expect(isExpired(SECRET, NOW)).toBe(false);
    expect(isExpired({ expires_at: "2026-10-07T00:00:00Z" }, NOW)).toBe(true);
    expect(isExpired({ expires_at: null }, NOW)).toBe(false);
  });
});
