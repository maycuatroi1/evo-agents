import { describe, expect, it } from "vitest";

import { branchUrl, commitUrl, repoWeb, splitLinks, webUrl } from "./forge";

const SHA = "7c1e9a2f3b4c5d6e7f8091a2b3c4d5e6f7081920";

describe("repoWeb", () => {
  it("reads a GitHub origin over https, without its .git, its user or its trailing slash", () => {
    expect(repoWeb("https://github.com/example-org/api.git")).toEqual({ url: "https://github.com/example-org/api", forge: "github" });
    expect(repoWeb("https://github.com/example-org/api")).toEqual({ url: "https://github.com/example-org/api", forge: "github" });
    expect(repoWeb("  https://x-access-token:ghs_secret@GitHub.com/example-org/api.git/ ")).toEqual({
      url: "https://github.com/example-org/api",
      forge: "github",
    });
    expect(repoWeb("https://www.github.com/example-org/api")?.url).toBe("https://github.com/example-org/api");
  });

  it("turns a GitHub origin over SSH, in git's scp form or as a URL, into its https page", () => {
    expect(repoWeb("git@github.com:example-org/api.git")).toEqual({ url: "https://github.com/example-org/api", forge: "github" });
    expect(repoWeb("ssh://git@github.com/example-org/api.git")).toEqual({ url: "https://github.com/example-org/api", forge: "github" });
    expect(repoWeb("git+ssh://git@github.com/example-org/api")).toEqual({ url: "https://github.com/example-org/api", forge: "github" });
  });

  it("reads GitLab on a host of its own over https and SSH, subgroups and the https port kept, the SSH port dropped", () => {
    expect(repoWeb("https://gitlab.example.org/group/sub/api.git")).toEqual({ url: "https://gitlab.example.org/group/sub/api", forge: "gitlab" });
    expect(repoWeb("https://gitlab.example.org:8443/group/api")).toEqual({ url: "https://gitlab.example.org:8443/group/api", forge: "gitlab" });
    expect(repoWeb("git@gitlab.example.org:group/sub/api.git")).toEqual({ url: "https://gitlab.example.org/group/sub/api", forge: "gitlab" });
    expect(repoWeb("ssh://git@gitlab.example.org:2222/group/api.git")).toEqual({ url: "https://gitlab.example.org/group/api", forge: "gitlab" });
    expect(repoWeb("http://gitlab.lan.example/group/api.git")).toEqual({ url: "http://gitlab.lan.example/group/api", forge: "gitlab" });
  });

  it("links another forge's repo page but knows none of its paths", () => {
    expect(repoWeb("git@git.example.net:team/api.git")).toEqual({ url: "https://git.example.net/team/api", forge: null });
    expect(repoWeb("https://bitbucket.org/team/api.git")).toEqual({ url: "https://bitbucket.org/team/api", forge: null });
    // A GitHub address that is not owner/repo is a page, not a repo whose branches the page can name.
    expect(repoWeb("https://github.com/example-org")).toEqual({ url: "https://github.com/example-org", forge: null });
  });

  it("gives nothing for an empty origin", () => {
    expect(repoWeb(null)).toBeNull();
    expect(repoWeb(undefined)).toBeNull();
    expect(repoWeb("")).toBeNull();
    expect(repoWeb("   ")).toBeNull();
  });

  it("gives nothing for a scheme that is not http, https or SSH, nor for a path on the machine", () => {
    expect(repoWeb("javascript:alert(1)")).toBeNull();
    expect(repoWeb("JavaScript://github.com/%0Aalert(1)")).toBeNull();
    expect(repoWeb("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(repoWeb("file:///srv/git/api.git")).toBeNull();
    expect(repoWeb("git://github.com/example-org/api.git")).toBeNull();
    expect(repoWeb("/srv/git/api.git")).toBeNull();
    expect(repoWeb("../api")).toBeNull();
    expect(repoWeb("C:/repos/api")).toBeNull();
    expect(repoWeb("https://localhost/group/api")).toBeNull();
  });

  it("gives nothing for an origin with characters no forge path holds", () => {
    expect(repoWeb("https://github.com/example-org/a pi")).toBeNull();
    expect(repoWeb('https://github.com/example-org/api"onmouseover="alert(1)')).toBeNull();
    expect(repoWeb("https://github.com/example-org/<api>")).toBeNull();
    expect(repoWeb("https://github.com/example-org/api\nhttps://evil.example")).toBeNull();
    expect(repoWeb("https://github.com/example-org/%2e%2e/api")).toBeNull();
    expect(repoWeb("https://github.com/example-org/../api")).toBeNull();
    expect(repoWeb("https://github.com/example-org/api?tab=readme")).toBeNull();
    expect(repoWeb("https://github.com/example-org/api#readme")).toBeNull();
    expect(repoWeb("https://github.com/")).toBeNull();
    expect(repoWeb("git@github.com:example-org/ápi.git")).toBeNull();
  });
});

describe("branchUrl and commitUrl", () => {
  const github = repoWeb("git@github.com:example-org/api.git");
  const gitlab = repoWeb("git@gitlab.example.org:group/sub/api.git");
  const other = repoWeb("git@git.example.net:team/api.git");

  it("lead to GitHub's /tree/ and /commit/ pages, a branch with slashes kept whole", () => {
    expect(branchUrl(github, "feat/hub-run-monitor")).toBe("https://github.com/example-org/api/tree/feat/hub-run-monitor");
    expect(branchUrl(github, "release#1")).toBe("https://github.com/example-org/api/tree/release%231");
    expect(commitUrl(github, SHA)).toBe(`https://github.com/example-org/api/commit/${SHA}`);
    expect(commitUrl(github, SHA.toUpperCase())).toBe(`https://github.com/example-org/api/commit/${SHA}`);
  });

  it("lead to GitLab's /-/tree/ and /-/commit/ pages", () => {
    expect(branchUrl(gitlab, "main")).toBe("https://gitlab.example.org/group/sub/api/-/tree/main");
    expect(commitUrl(gitlab, SHA.slice(0, 12))).toBe(`https://gitlab.example.org/group/sub/api/-/commit/${SHA.slice(0, 12)}`);
  });

  it("give nothing on another forge, without a repo, or for a name git would refuse", () => {
    expect(branchUrl(other, "main")).toBeNull();
    expect(commitUrl(other, SHA)).toBeNull();
    expect(branchUrl(null, "main")).toBeNull();
    expect(commitUrl(null, SHA)).toBeNull();
    expect(branchUrl(github, null)).toBeNull();
    expect(branchUrl(github, "")).toBeNull();
    for (const name of ["a b", "a..b", "a~1", "a^", "a:b", "a?", "a*", "a[b", "a\\b", "-main", "/main", "main/", "a//b", "main.lock", ".hidden", "a@{1}", "a\nb"]) {
      expect(branchUrl(github, name), name).toBeNull();
    }
    for (const sha of ["", "abc", "7c1e9a2z", "javascript:alert(1)", `${SHA}0`.repeat(2)]) {
      expect(commitUrl(github, sha), sha).toBeNull();
    }
  });
});

describe("webUrl", () => {
  it("takes a whole http or https address and nothing else", () => {
    expect(webUrl("https://github.com/example-org/api")).toBe("https://github.com/example-org/api");
    expect(webUrl("http://ci.example.org/job/7")).toBe("http://ci.example.org/job/7");
    expect(webUrl("javascript:alert(1)")).toBeNull();
    expect(webUrl("data:text/html,hi")).toBeNull();
    expect(webUrl("file:///etc/passwd")).toBeNull();
    expect(webUrl("ftp://example.org/x")).toBeNull();
    expect(webUrl("https://")).toBeNull();
    expect(webUrl("https://example.org/a b")).toBeNull();
    expect(webUrl("CLAUDE_CODE_OAUTH_TOKEN")).toBeNull();
    expect(webUrl(null)).toBeNull();
  });
});

describe("splitLinks", () => {
  const links = (text: string) => splitLinks(text).filter((piece) => piece.href !== null);

  it("cuts text into its words and its addresses, and joins back into the text exactly", () => {
    const text = "Opened https://github.com/example-org/api/pull/7, then (see http://ci.example.org/job/7).\nDone.";
    const pieces = splitLinks(text);
    expect(pieces.map((piece) => piece.text).join("")).toBe(text);
    expect(links(text)).toEqual([
      { text: "https://github.com/example-org/api/pull/7", href: "https://github.com/example-org/api/pull/7" },
      { text: "http://ci.example.org/job/7", href: "http://ci.example.org/job/7" },
    ]);
  });

  it("keeps brackets an address opened, and stops at quotes and angle brackets", () => {
    expect(links("see https://en.wikipedia.org/wiki/Queue_(abstract_data_type).")[0].text).toBe(
      "https://en.wikipedia.org/wiki/Queue_(abstract_data_type)",
    );
    expect(links('{"url":"https://example.org/a?b=1&c=2"}')[0].href).toBe("https://example.org/a?b=1&c=2");
    expect(links("<https://example.org/x>")[0].text).toBe("https://example.org/x");
  });

  it("never links another scheme, and leaves text without an address as one piece", () => {
    expect(links("javascript:alert(1) data:text/html,x file:///etc/passwd ftp://example.org")).toEqual([]);
    expect(links("xhttps://example.org")).toEqual([]);
    expect(splitLinks("no address here")).toEqual([{ text: "no address here", href: null }]);
    expect(splitLinks("")).toEqual([{ text: "", href: null }]);
  });
});
