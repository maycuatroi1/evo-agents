// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { EntitySetup } from "@/lib/kg/entity-graph";
import { entityKeys } from "@/lib/kg/entity-queries";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { EntitiesEmpty } from "./entity-empty";

const api = vi.hoisted(() => ({
  setup: null as null | Record<string, unknown>,
  posted: null as null | Record<string, unknown>,
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (request.method === "POST") return Response.json(api.posted, { status: 202 });
        return Response.json(api.setup);
      },
    }),
}));

const build = (id: number, status: "queued" | "running") => ({
  id,
  status,
  job_id: 3,
  requested_by: "wendy",
  config_digest: null,
  runs: null,
  artifact_sha256: null,
  artifact_size: null,
  artifact_reused_from: null,
  artifact_pruned_at: null,
  content_hash: null,
  nodes: null,
  edges: null,
  error: null,
  queued_at: "2026-11-05T16:40:00Z",
  started_at: null,
  finished_at: null,
});

const OFF: EntitySetup = {
  strategy: null,
  step_on: false,
  turns_on: "entity-graph-api",
  sink: "llm",
  sink_problem: null,
  can_change: true,
  building: null,
};

function renderEmpty(setup: EntitySetup) {
  api.setup = setup;
  const client = makeQueryClient();
  client.setQueryData(entityKeys.setup("demo"), setup);
  renderVi(
    <QueryClientProvider client={client}>
      <EntitiesEmpty project="demo" documents={1000} />
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  api.seen = [];
  api.posted = null;
});

describe("EntitiesEmpty", () => {
  it("turns the step on and queues a build in one click, then says the build is under way", async () => {
    const user = userEvent.setup();
    api.posted = { ...OFF, strategy: "entity-graph-api", step_on: true, building: build(19944, "queued"), queued: true };
    renderEmpty(OFF);
    const empty = await screen.findByTestId("kg-entities-empty");
    expect(within(empty).getByTestId("kg-entities-estimate").textContent).toContain("1.000");
    await user.click(within(empty).getByTestId("kg-entities-build"));
    await waitFor(() => expect(api.seen.some((r) => r.method === "POST" && r.url.endsWith("/v1/kg/demo/entities/setup"))).toBe(true));
    expect(api.seen.find((r) => r.method === "POST")!.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    await waitFor(() => expect(screen.getByTestId("kg-entities-reason").textContent).toContain("#19944"));
    expect(screen.queryByTestId("kg-entities-build")).toBeNull();
  });

  it("offers a build of the step already on", async () => {
    renderEmpty({ ...OFF, strategy: "entity-graph-api", step_on: true });
    expect((await screen.findByTestId("kg-entities-build")).textContent).toBe("Build ngay");
  });

  it("names the sink knowledge.yaml lacks and keeps the button off", async () => {
    renderEmpty({ ...OFF, sink_problem: "sink 'llm' is not declared in knowledge.yaml policy.sinks" });
    expect((await screen.findByTestId("kg-entities-sink")).textContent).toContain("llm");
    expect(screen.getByTestId("kg-entities-build")).toBeDisabled();
  });

  it("tells a reader a writer turns it on", async () => {
    renderEmpty({ ...OFF, can_change: false });
    expect(await screen.findByTestId("kg-entities-ask")).toBeTruthy();
    expect(screen.queryByTestId("kg-entities-build")).toBeNull();
  });

  it("reads the graph again once the build in flight is done", async () => {
    const client = renderEmpty({ ...OFF, step_on: true, building: build(19944, "running") });
    const reread = vi.spyOn(client, "invalidateQueries");
    await screen.findByTestId("kg-entities-empty");
    client.setQueryData(entityKeys.setup("demo"), { ...OFF, step_on: true });
    await waitFor(() => expect(reread).toHaveBeenCalledWith({ queryKey: ["kg", "demo"] }));
  });
});
