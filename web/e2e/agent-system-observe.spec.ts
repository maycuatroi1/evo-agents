import type { Locator, Page, Route } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { chainSpec, putSystemByApi, systemPath } from "./support/systems";

/**
 * Observe mode of an agent system and the page of one trace (web/DESIGN.md, Agent systems). The system is real; its
 * traces, metrics and trace stream are answered by the page's routes, since no worker runs agents in this stack. The
 * canvas shows each node's state and numbers, counts on the edges work crossed, an off-design crossing as a dashed
 * edge to a node outside the design, and the recent traces; a trace that moves arrives through the stream without a
 * reload, a hidden tab stops the stream, and a trace's page lays out its spans by agent: runs, messages, kg queries,
 * cost. Nothing says the stream is live while all is well. Pages render in English.
 */
test.skip(isDeployed, "creates agents and systems through the local stack");

type Span = Record<string, unknown> & { id: number; started_at: string };

function main(page: Page) {
  return page.locator("#main");
}

/** Observe's canvas: the designer, opened first, stays mounted and hidden under it with nodes and edges of its own. */
function canvas(page: Page): Locator {
  return main(page).getByTestId("system-observe-canvas");
}

function node(page: Page, id: string): Locator {
  return canvas(page).locator(`[data-testid="system-node"][data-node="${id}"]`);
}

function edge(page: Page, id: string): Locator {
  return canvas(page).locator(`[data-testid="system-edge"][data-edge="${id}"]`);
}

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function span(id: number, patch: Partial<Span>): Span {
  return {
    id,
    parent_id: null,
    name: "invoke_agent",
    tool: null,
    agent: "",
    node: null,
    from_node: null,
    to_node: null,
    run_id: null,
    project: null,
    runtime: "claude",
    status: "ok",
    off_design: false,
    reason: null,
    message_id: null,
    attributes: {},
    started_at: ago(120_000),
    ended_at: ago(100_000),
    seconds: 20,
    input_tokens: 0,
    output_tokens: 0,
    usd: 0,
    model: null,
    run_state: "done",
    content_withheld: false,
    ...patch,
  };
}

function summary(id: number, slug: string, spans: Span[]) {
  const runs = spans.filter((one) => one.name === "invoke_agent");
  return {
    id,
    system: slug,
    revision: 1,
    chat_id: 7,
    message_id: 70 + id,
    opened_by: "rin",
    started_at: spans[0].started_at,
    last_at: ago(1_000),
    runs: runs.length,
    off_design: spans.filter((one) => one.off_design).length,
    errors: spans.filter((one) => one.status === "error" || one.status === "refused").length,
    usd: runs.reduce((sum, one) => sum + Number(one.usd), 0),
  };
}

test("Observe shows each node's state and numbers, the edges work crossed, the recent traces, and a trace's waterfall", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const triage = uniqueName("triage");
  const billing = uniqueName("billing");
  const legal = `@${uniqueName("legal")}`;
  await createAgentByApi(me, project, { handle: triage, name: "Triage", permissions: {} });
  await createAgentByApi(me, project, { handle: billing, name: "Billing", permissions: {} });
  const slug = uniqueName("desk");
  await putSystemByApi(me, project, slug, chainSpec("Support desk", [{ handle: triage, version: 1 }, { handle: billing, version: 1 }]));

  // Trace 41 ended: triage asked billing, billing queried the docs graph, asked an agent outside the design (refused)
  // and failed. Trace 42 opens later, through the stream: triage works on a new message.
  const older = [
    span(1, { agent: triage, node: triage, from_node: "entry", run_id: 501, project, model: "sonnet", input_tokens: 1200, output_tokens: 300, usd: 0.0123, message_id: 71 }),
    span(2, { parent_id: 1, name: "execute_tool", tool: "agent_ask", agent: triage, node: triage, to_node: billing, run_state: null, started_at: ago(110_000), ended_at: ago(110_000), seconds: 0, message_id: 72 }),
    span(3, { parent_id: 2, agent: billing, node: billing, from_node: triage, run_id: 502, project, status: "error", run_state: "failed", started_at: ago(109_000), ended_at: ago(90_000), usd: 0.004 }),
    span(4, { parent_id: 3, name: "execute_tool", tool: "kg_search", agent: billing, node: billing, run_state: null, attributes: { "gen_ai.data_source.id": "docs", "evo.kg.results": 3 }, started_at: ago(105_000), ended_at: ago(104_000), seconds: 1 }),
    span(5, { parent_id: 3, name: "execute_tool", tool: "agent_ask", agent: billing, node: billing, to_node: legal, status: "refused", off_design: true, reason: "no edge to this agent", run_state: null, started_at: ago(100_000), ended_at: ago(100_000), seconds: 0 }),
  ];
  const newer = [span(6, { agent: triage, node: triage, from_node: "entry", run_id: 503, project, status: "running", run_state: "running", started_at: ago(2_000), ended_at: null, seconds: null })];
  const metrics = {
    nodes: [
      { node: triage, count: 4, errors: 0, off_design: 0, p50_seconds: 20, input_tokens: 4000, output_tokens: 900, usd: 0.05 },
      { node: billing, count: 3, errors: 1, off_design: 0, p50_seconds: 19, input_tokens: 2000, output_tokens: 400, usd: 0.02 },
    ],
    edges: [
      { from_node: "entry", to_node: triage, count: 4, errors: 0, off_design: 0 },
      { from_node: triage, to_node: billing, count: 3, errors: 1, off_design: 0 },
      { from_node: billing, to_node: legal, count: 1, errors: 1, off_design: 1 },
    ],
  };
  const traces = new Map<number, Span[]>([[41, older]]);

  // The stream: empty until the test releases trace 42, which goes out once; each empty answer makes it reconnect.
  let release = false;
  let sent = false;
  let streams = 0;
  const base = `/v1/projects/${project}/systems/${slug}`;
  await page.route(`**${base}/**`, async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (path === `${base}/traces/stream`) {
      streams += 1;
      let body = "retry: 300\n\n";
      if (release && !sent) {
        sent = true;
        traces.set(42, newer);
        body += `event: trace\ndata: ${JSON.stringify(summary(42, slug, newer))}\n\n`;
      }
      return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream", "cache-control": "no-cache" }, body });
    }
    if (path === `${base}/traces`) return json([...traces.entries()].reverse().map(([id, spans]) => summary(id, slug, spans)));
    if (path === `${base}/metrics`) return json(metrics);
    const one = /\/traces\/(\d+)$/.exec(path);
    const spans = one ? traces.get(Number(one[1])) : undefined;
    if (spans) return json({ ...summary(Number(one?.[1]), slug, spans), spans, metrics });
    return route.continue();
  });

  await open(page, systemPath(project, slug));
  await main(page).getByTestId("system-mode-observe").click();
  await page.waitForURL(`**${systemPath(project, slug)}?mode=observe`);
  const observe = main(page).getByTestId("system-observe");
  await expect(observe).toHaveAttribute("data-revision", "1");
  await expect(main(page).getByTestId("system-mode-observe")).toHaveAttribute("aria-current", "page");

  // Each node's state and numbers.
  await expect(node(page, billing).getByTestId("system-node-state")).toHaveAttribute("data-state", "failed");
  await expect(node(page, triage).getByTestId("system-node-state")).toHaveAttribute("data-state", "idle");
  await expect(node(page, "entry").getByTestId("system-node-state")).toHaveAttribute("data-state", "idle");
  await expect(node(page, triage).getByTestId("system-node-numbers")).toContainText("4 runs");
  await expect(node(page, billing).getByTestId("system-node-numbers")).toContainText("3 runs");
  await expect(node(page, "entry").getByTestId("system-node-numbers")).toHaveText("1 trace");

  // Counts on the edges; the crossing the design lacks is a dashed edge to a node outside it.
  await expect(edge(page, "e0")).toHaveAttribute("data-count", "4");
  await expect(edge(page, "e1")).toHaveAttribute("data-count", "3");
  await expect(edge(page, "e1")).toHaveAttribute("data-off-design", "false");
  await expect(edge(page, "e0")).toHaveAttribute("data-lit", "false");
  const off = edge(page, `x-${billing}-${legal}`);
  await expect(off).toHaveAttribute("data-off-design", "true");
  await expect(off).toContainText("Off design");
  await expect(node(page, legal).getByTestId("system-node-numbers")).toHaveText("Not in the design");

  // The node's numbers in full.
  await node(page, billing).click();
  const detail = main(page).getByTestId("system-node-detail");
  await expect(detail).toHaveAttribute("data-node", billing);
  await expect(detail).toContainText("Failed");
  await expect(detail).toContainText("2,400");

  // The recent traces; nothing says the stream is live.
  const rows = main(page).getByTestId("system-trace-row");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toHaveAttribute("data-trace", "41");
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);
  await expect(observe).toHaveAttribute("data-stream", "live");

  // A new trace arrives through the stream: it heads the list, triage works and the entry's edge lights up.
  release = true;
  await expect(rows.first()).toHaveAttribute("data-trace", "42", { timeout: 10_000 });
  await expect(rows).toHaveCount(2);
  await expect(node(page, triage).getByTestId("system-node-state")).toHaveAttribute("data-state", "working");
  await expect(node(page, "entry").getByTestId("system-node-state")).toHaveAttribute("data-state", "working");
  await expect(edge(page, "e0")).toHaveAttribute("data-lit", "true");
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);

  // A hidden tab stops the stream; showing it again opens it.
  const setVisibility = (state: "hidden" | "visible") =>
    page.evaluate((value) => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
      document.dispatchEvent(new Event("visibilitychange"));
    }, state);
  await setVisibility("hidden");
  await expect(observe).toHaveAttribute("data-stream", "paused");
  const whileHidden = streams;
  await page.waitForTimeout(1_000);
  expect(streams).toBe(whileHidden);
  await setVisibility("visible");
  await expect(observe).toHaveAttribute("data-stream", "live");
  await expect.poll(() => streams).toBeGreaterThan(whileHidden);
  await expect(main(page).getByTestId("system-observe-stream")).toHaveCount(0);

  // The trace's page: its totals, the work of each agent, and the waterfall of its spans.
  await main(page).locator('[data-testid="system-trace-row"][data-trace="41"]').getByRole("link").click();
  await page.waitForURL(`**${systemPath(project, slug)}/traces/41`);
  const trace = main(page).getByTestId("system-trace-page");
  await expect(trace.getByRole("heading", { level: 1 })).toContainText("41");
  await expect(trace.getByTestId("system-trace-summary")).toContainText("$0.0163");
  await expect(trace.locator(`[data-testid="system-trace-agent"][data-agent="${triage}"]`)).toContainText("$0.0123");
  await expect(trace.locator(`[data-testid="system-trace-agent"][data-agent="${billing}"]`)).toContainText("2 failed");
  const spans = trace.getByTestId("system-waterfall-span");
  await expect(spans).toHaveCount(5);
  expect(await spans.evaluateAll((rows) => rows.map((row) => row.getAttribute("data-kind")))).toEqual(["run", "message", "run", "kg", "message"]);
  await expect(spans.nth(0)).toContainText("sonnet");
  await expect(spans.nth(0).getByRole("link", { name: "Run 501" })).toHaveAttribute("href", `/p/${project}/runs/501`);
  await expect(spans.nth(1).getByRole("link", { name: "Message" })).toHaveAttribute("href", /\/chats\/7/);
  await expect(spans.nth(3)).toContainText("docs");
  await expect(spans.nth(3)).toContainText("3 results");
  await expect(spans.nth(4)).toHaveAttribute("data-off-design", "true");
  await expect(spans.nth(4)).toHaveAttribute("data-status", "refused");
  await expect(spans.nth(4)).toContainText("no edge to this agent");
  await expect(spans.nth(2)).toHaveAttribute("data-status", "error");
  await expect(trace.getByTestId("system-waterfall-bar")).toHaveCount(5);

  // Back to Observe.
  await trace.getByRole("link", { name: new RegExp(slug) }).first().click();
  await page.waitForURL(`**${systemPath(project, slug)}?mode=observe`);
  await expect(main(page).getByTestId("system-observe")).toBeVisible();
});
