// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { AgentTrace, type TraceContext } from "./agent-trace";
import { LinkedText } from "./links";
import type { Run, RunEvent } from "./queries";
import { RunCredentials } from "./run-credentials";
import { RunDetails, RunResult } from "./run-facts";

/** The references of a run's page as links: the repo, its branch and the commit on the forge, and the addresses in text. */

const api = vi.hoisted(() => ({ origin: null as string | null | undefined, leases: [] as unknown[] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        if (path === "/v1/projects/demo") {
          const repos = api.origin === undefined ? [] : [{ name: "api", origin: api.origin, default_branch: "main", path: "api" }];
          return Response.json({ name: "demo", harness: null, levels: [], locations: [], default_label: {}, sinks: [], repos, role: "writer", max_level: null });
        }
        if (path === "/v1/projects/demo/runs/12/credentials") return Response.json(api.leases);
        return Response.json({ error: "not_found", message: path }, { status: 404 });
      },
    }),
}));

const SHA = "7c1e9a2f3b4c5d6e7f8091a2b3c4d5e6f7081920";

const run = {
  id: 12,
  project: "demo",
  kind: "step",
  state: "review",
  plan_id: "rollout",
  step_key: "2",
  repo: "api",
  branch: "feat/queue",
  repos: null,
  worker: null,
  worker_id: null,
  pinned_worker_id: null,
  runtime: "codex",
  requested_runtime: "codex",
  mode: "headless",
  model: null,
  session_id: null,
  lease_expires_at: null,
  attempt: 1,
  max_attempts: 2,
  parent_run_id: null,
  resume_of_run_id: null,
  approval: "review",
  timeout_min: 60,
  run_seconds: 0,
  dispatched_by: "octo",
  queued_at: "2026-10-07T03:00:00Z",
  leased_at: null,
  started_at: null,
  waiting_since: null,
  parked_at: null,
  finished_at: null,
  plan_revision: null,
  commit_sha: SHA,
  diffstat: null,
  diff_sha256: null,
  verify: [{ command: "curl -fsS https://ci.example.org/health", exit_code: 0, duration_ms: 900 }],
  evidence: "Opened https://github.com/example-org/api/pull/7, then javascript:alert(1).",
  error: null,
  usage: null,
} as unknown as Run;

function show(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** Each link out of the hub opens a new tab, without opener or referrer, and its name says so after its text. */
function expectExternal(link: HTMLElement, href: string, text: string) {
  expect(link).toHaveAttribute("href", href);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(link).toHaveAccessibleName(`${text} (mở trong tab mới)`);
  expect(link).toHaveTextContent(text);
}

beforeEach(() => {
  api.origin = "git@github.com:example-org/api.git";
  api.leases = [];
});

describe("a run's repo, branch and commit", () => {
  it("link to their pages on GitHub, read from the SSH origin the project registered", async () => {
    show(
      <>
        <RunDetails run={run} viewer={null} />
        <RunResult run={run} />
      </>,
    );
    const repo = await within(screen.getByTestId("run-details")).findByTestId("run-repo-link");
    expectExternal(repo, "https://github.com/example-org/api", "api");
    expectExternal(within(screen.getByTestId("run-details")).getByTestId("run-branch"), "https://github.com/example-org/api/tree/feat/queue", "feat/queue");
    const result = screen.getByTestId("run-result");
    expectExternal(within(result).getByTestId("run-commit-sha"), `https://github.com/example-org/api/commit/${SHA}`, "7c1e9a2");
    expectExternal(within(result).getByTestId("run-commit-branch"), "https://github.com/example-org/api/tree/feat/queue", "feat/queue");
    // The copy button stays beside the commit's link.
    expect(within(result).getByTestId("identifier-copy")).toBeInTheDocument();
  });

  it("link to GitLab's /-/ pages on a host of its own", async () => {
    api.origin = "https://gitlab.example.org/group/sub/api.git";
    show(<RunResult run={run} />);
    const commit = await screen.findByRole("link", { name: /^7c1e9a2/ });
    expect(commit).toHaveAttribute("href", `https://gitlab.example.org/group/sub/api/-/commit/${SHA}`);
    expect(screen.getByTestId("run-commit-branch")).toHaveAttribute("href", "https://gitlab.example.org/group/sub/api/-/tree/feat/queue");
  });

  it("on a forge the page does not know, link the repo alone", async () => {
    api.origin = "ssh://git@git.example.net:2222/team/api.git";
    show(
      <>
        <RunDetails run={run} viewer={null} />
        <RunResult run={run} />
      </>,
    );
    expectExternal(await screen.findByTestId("run-repo-link"), "https://git.example.net/team/api", "api");
    expect(screen.getByTestId("run-branch").tagName).toBe("SPAN");
    expect(screen.getByTestId("run-commit-sha").tagName).toBe("SPAN");
    expect(screen.queryByTestId("run-commit-branch")).toBeNull();
  });

  it("stay text without an origin, or with one the page cannot read as a web page", async () => {
    for (const origin of [null, "file:///srv/git/api.git", "javascript:alert(1)//github.com/a/b"]) {
      api.origin = origin;
      const view = show(
        <>
          <RunDetails run={run} viewer={null} />
          <RunResult run={run} />
        </>,
      );
      expect(await screen.findByTestId("run-repo-name")).toHaveTextContent("api");
      expect(screen.queryByTestId("run-repo-link")).toBeNull();
      expect(screen.getByTestId("run-branch").tagName).toBe("SPAN");
      expect(screen.getByTestId("run-commit-sha").tagName).toBe("SPAN");
      expect(within(screen.getByTestId("run-details")).queryAllByRole("link")).toEqual([]);
      view.unmount();
    }
  });

  it("show a plan run's repos each with its own link, and a repo the project does not list as text", async () => {
    const planRun = { ...run, kind: "plan", step_key: null, repo: null, branch: null, commit_sha: null, repos: [{ repo: "api", branch: "main" }, { repo: "harness", branch: null }] } as unknown as Run;
    show(<RunDetails run={planRun} viewer={null} />);
    const repos = screen.getByTestId("run-repos");
    expectExternal(await within(repos).findByTestId("run-repo-link"), "https://github.com/example-org/api", "api");
    expect(within(repos).getByTestId("run-branch")).toHaveAttribute("href", "https://github.com/example-org/api/tree/main");
    expect(within(repos).getByTestId("run-repo-name")).toHaveTextContent("harness");
    expect(within(repos).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["apitrên nhánh main", "harness"]);
  });
});

describe("the Result card", () => {
  it("links every address in the verify commands and the evidence, and never another scheme", async () => {
    show(<RunResult run={run} />);
    const result = screen.getByTestId("run-result");
    expectExternal(within(within(result).getByTestId("run-verify")).getByRole("link"), "https://ci.example.org/health", "https://ci.example.org/health");
    const evidence = within(result).getByTestId("run-evidence");
    expect(evidence).toHaveTextContent("Opened https://github.com/example-org/api/pull/7, then javascript:alert(1).");
    const links = within(evidence).getAllByRole("link");
    expect(links).toHaveLength(1);
    expectExternal(links[0], "https://github.com/example-org/api/pull/7", "https://github.com/example-org/api/pull/7");
  });

  it("is its title and one short line while there is nothing to show", () => {
    const empty = { ...run, state: "running", commit_sha: null, verify: null, evidence: null } as unknown as Run;
    const first = show(<RunResult run={empty} />);
    expect(screen.getByTestId("run-result-empty")).toHaveTextContent(/^Chưa có$/);
    expect(screen.getByTestId("run-result").querySelector("dl")).toBeNull();
    first.unmount();
    show(<RunResult run={{ ...empty, state: "failed" } as Run} />);
    expect(screen.getByTestId("run-result-empty")).toHaveTextContent(/^Không có$/);
  });
});

describe("the Credentials card", () => {
  it("links the repos a git lease answered for, keeps a variable as text, and links the Secrets page in its head", async () => {
    api.leases = [
      { id: 1, name: "github-org", provider: "secret", kind: "git", target: "https://github.com/example-org/api", worker: "laptop", issued_at: "2026-10-07T03:00:00Z", expires_at: null, revoked_at: null },
      { id: 2, name: "claude-oauth", provider: "secret", kind: "env", target: "CLAUDE_CODE_OAUTH_TOKEN", worker: "laptop", issued_at: "2026-10-07T03:00:00Z", expires_at: null, revoked_at: null },
    ];
    show(<RunCredentials run={run} />);
    const card = screen.getByTestId("run-credentials");
    const items = await within(card).findAllByTestId("run-lease-item");
    expectExternal(within(items[0]).getByTestId("run-lease-repo-link"), "https://github.com/example-org/api", "https://github.com/example-org/api");
    expect(within(items[0]).getByTestId("run-lease-target")).toHaveTextContent(/^https:\/\/github\.com\/example-org\/api$/);
    expect(within(items[1]).getByTestId("run-lease-target")).toHaveTextContent("CLAUDE_CODE_OAUTH_TOKEN");
    expect(within(items[1]).queryByRole("link")).toBeNull();
    const secrets = within(card).getByTestId("run-credentials-secrets-link");
    expect(secrets).toHaveAttribute("href", "/secrets");
    expect(secrets).toHaveAccessibleName("Secret");
    expect(card).not.toHaveTextContent("không bao giờ có value");
  });
});

describe("LinkedText and the trace", () => {
  it("leaves text without an address as it is, and never links javascript: or data:", () => {
    show(<LinkedText text="javascript:alert(1) data:text/html,x and nothing else" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(document.body).toHaveTextContent("javascript:alert(1) data:text/html,x and nothing else");
  });

  it("links the addresses of the agent's thinking, a tool's output, and a fetch's address once its row is open", async () => {
    const context: TraceContext = { runId: 12, owner: "octo", viewer: "octo", describe: (move) => `${move.from} > ${move.to}`, openDecisions: new Set(), working: false, active: false };
    let seq = 0;
    const ev = (kind: string, body: Record<string, unknown>) => ({ seq: ++seq, at: "2026-10-07T03:00:00.000Z", kind, body, truncated: false });
    const text = (value: string) => ({ type: "text", text: value });
    const events = [
      ev("agent_thought_chunk", { content: text("The docs at https://docs.example.org/queue say so.") }),
      ev("agent_message_chunk", { content: text("Done.") }),
      ev("tool_call", { toolCallId: "t1", title: "Bash", kind: "execute", status: "pending", rawInput: { command: "gh pr create" } }),
      ev("tool_call_update", { toolCallId: "t1", status: "failed", rawOutput: { exitCode: 1 }, content: [{ type: "content", content: text("Opened https://github.com/example-org/api/pull/7\njavascript:alert(1)") }] }),
      ev("tool_call", { toolCallId: "t2", title: "WebFetch", kind: "fetch", status: "completed", rawInput: { url: "https://docs.example.org/queue" } }),
    ];
    show(<AgentTrace events={events as RunEvent[]} status="ended" context={context} />);
    const tools = screen.getAllByTestId("trace-tool");
    const output = within(tools[0]).getByTestId("trace-tool-output");
    expect(within(output).getAllByRole("link")).toHaveLength(1);
    expectExternal(within(output).getByRole("link"), "https://github.com/example-org/api/pull/7", "https://github.com/example-org/api/pull/7");
    expect(output).toHaveTextContent("javascript:alert(1)");

    // The fetch's folded line is text (a link would open and close the row); open, its Input links the address.
    expect(within(tools[1]).getByTestId("trace-tool-arg")).toHaveTextContent("https://docs.example.org/queue");
    expect(within(tools[1]).queryByRole("link")).toBeNull();
    await userEvent.setup().click(within(tools[1]).getByTestId("trace-tool-arg"));
    expectExternal(within(within(tools[1]).getByTestId("trace-tool-input")).getByRole("link"), "https://docs.example.org/queue", "https://docs.example.org/queue");

    const thought = screen.getByTestId("trace-thought");
    expectExternal(within(thought).getByRole("link"), "https://docs.example.org/queue", "https://docs.example.org/queue");
  });
});
