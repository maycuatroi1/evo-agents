import { describe, expect, it } from "vitest";

import { branchUrl, webBase } from "./forge";

describe("forge links of the Curator", () => {
  it("reads a repo's web page from an https, http or scp origin, and nothing else", () => {
    expect(webBase("https://github.com/org/api.git")).toBe("https://github.com/org/api");
    expect(webBase("http://gitlab.example.org/group/sub/api/")).toBe("https://gitlab.example.org/group/sub/api");
    expect(webBase("git@github.com:org/api.git")).toBe("https://github.com/org/api");
    expect(webBase("file:///srv/api")).toBeNull();
    expect(webBase("not a url")).toBeNull();
    expect(webBase(null)).toBeNull();
  });

  it("links a change's branch on GitHub and GitLab, each part of it escaped", () => {
    expect(branchUrl("https://github.com/org/api", "curator/12-wait-helper", "github")).toBe("https://github.com/org/api/tree/curator/12-wait-helper");
    expect(branchUrl("git@gitlab.example.org:group/api.git", "curator/3-docs", "gitlab")).toBe("https://gitlab.example.org/group/api/-/tree/curator/3-docs");
    expect(branchUrl("https://github.com/org/api", "curator/7-a#b", "github")).toBe("https://github.com/org/api/tree/curator/7-a%23b");
  });

  it("leaves a branch as text without a forge, an origin, or a name git takes", () => {
    expect(branchUrl("https://github.com/org/api", "curator/1-x", null)).toBeNull();
    expect(branchUrl(null, "curator/1-x", "github")).toBeNull();
    expect(branchUrl("https://github.com/org/api", null, "github")).toBeNull();
    expect(branchUrl("https://github.com/org/api", "curator/../main", "github")).toBeNull();
    expect(branchUrl("https://github.com/org/api", "curator/a b", "github")).toBeNull();
  });
});
