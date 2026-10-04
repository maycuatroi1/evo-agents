import { describe, expect, it } from "vitest";

import { githubCommitUrl, githubRepo } from "./source";

const COMMIT = "0123abcd";

describe("githubCommitUrl", () => {
  it("links owner/repo and GitHub origins to the commit", () => {
    for (const repo of [
      "example-org/agent-skills",
      "https://github.com/example-org/agent-skills",
      "https://github.com/example-org/agent-skills.git",
      "git@github.com:example-org/agent-skills.git",
      "ssh://git@github.com/example-org/agent-skills",
      "github.com/example-org/agent-skills/",
    ]) {
      expect(githubCommitUrl(repo, COMMIT), repo).toBe("https://github.com/example-org/agent-skills/commit/0123abcd");
    }
  });

  it("links a bare repo name through the project's repo of that name", () => {
    const repos = [
      { name: "agent-skills", origin: "git@github.com:example-org/agent-skills.git" },
      { name: "internal", origin: "https://gitlab.example.org/team/internal" },
    ];
    expect(githubCommitUrl("agent-skills", COMMIT, repos)).toBe(
      "https://github.com/example-org/agent-skills/commit/0123abcd",
    );
    expect(githubCommitUrl("internal", COMMIT, repos)).toBeNull(); // not on GitHub
    expect(githubCommitUrl("agent-skills", COMMIT)).toBeNull(); // no project to ask
  });

  it("links nothing it cannot be sure of", () => {
    expect(githubCommitUrl(null, COMMIT)).toBeNull();
    expect(githubCommitUrl("example-org/x", null)).toBeNull();
    expect(githubCommitUrl("example-org/x", "not-hex")).toBeNull();
    expect(githubCommitUrl("https://gitlab.com/a/b", COMMIT)).toBeNull();
    expect(githubCommitUrl("https://github.com.evil.example/a/b", COMMIT)).toBeNull();
    expect(githubCommitUrl("javascript:alert(1)//github.com/a/b", COMMIT)).toBeNull();
    expect(githubCommitUrl("a/b/c", COMMIT)).toBeNull();
    expect(githubRepo("example-org/..")).toBeNull();
  });
});
