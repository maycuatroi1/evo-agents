import { cspSources } from "./support/csp";
import { expect, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import {
  changeKg,
  CONTRACT,
  graphReady,
  grantOn,
  HUB_NODE,
  kgContext,
  kgPath,
  kgProject,
  neighbourRows,
  nodePath,
  NOTES,
  queueBuild,
  RUNBOOK,
  SHARED_NODE,
  seededKg,
  sharedKg,
  open,
} from "./support/kg";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * The knowledge graph pages against a graph the stack built from the synthetic fixture: build status with the
 * build's content hash, node search, the neighbour table beside the canvas (and its match with kg_context on the
 * same store), the cut at 150 nodes, label filtering for an internal reader, and the small-screen layout.
 */
test.describe("knowledge graph", () => {
  test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds a graph through the local stack");

  test("the status shows the fixture build's content hash, size and an empty queue", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const reader = newAccount("kg-reader");
    await grantOn(kg.project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, kgPath(kg.project));

    await expect(page.getByRole("heading", { level: 1, name: "Đồ thị tri thức" })).toBeVisible();
    const latest = page.getByTestId("kg-latest-build");
    await expect(latest).toHaveAttribute("data-build-id", String(kg.build.id));
    await expect(latest.getByTestId("build-status")).toHaveText("Thành công");
    await expect(latest.getByTestId("build-status")).toHaveAttribute("data-status", "succeeded");
    await expect(latest.getByTestId("latest-content-hash")).toHaveText(kg.build.content_hash!);
    await expect(latest.getByTestId("build-artifact")).toHaveAttribute("data-state", "own");
    await expect(page.getByTestId("tile-hash").locator("[title]").first()).toHaveAttribute("title", kg.build.content_hash!);
    await expect(page.getByTestId("tile-size")).toContainText(`${kg.build.nodes} node`);
    await expect(page.getByTestId("queue-empty")).toBeVisible();
    await expect(page.getByTestId("kg-build-history").getByRole("row")).toHaveCount(2); // header and the build
    // Read-only: nothing on the page queues a build.
    await expect(page.getByTestId("kg-read-only")).toBeVisible();
    await expect(page.getByRole("button", { name: /xếp build|chạy build|build lại/i })).toHaveCount(0);
    // The sidebar links here.
    const nav = page.getByRole("navigation", { name: "Điều hướng chính" });
    await expect(nav.getByRole("link", { name: "Đồ thị tri thức" })).toHaveAttribute("aria-current", "page");
  });

  test("search finds the fixture node and opens it", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const reader = newAccount("kg-reader");
    await grantOn(kg.project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, kgPath(kg.project));
    await expect(page.getByTestId("kg-kinds")).toContainText("Document");

    await page.getByLabel("Từ khoá").fill("runbook");
    await page.getByRole("button", { name: "Tìm" }).click();
    await page.waitForURL((url) => url.searchParams.get("q") === "runbook");
    const first = page.getByTestId("kg-result-link").first();
    await expect(first).toHaveAttribute("data-node-id", RUNBOOK);
    await expect(page.getByTestId("kg-results-count")).toContainText("kết quả cho “runbook”");

    await first.click();
    await page.waitForURL((url) => url.pathname.endsWith("/kg/node") && url.searchParams.get("id") === RUNBOOK);
    await expect(page.getByTestId("kg-node-title")).toHaveText("runbook");
    await expect(page.getByTestId("node-source")).toHaveText("docs");
    await expect(page.getByTestId("node-uri").first()).toHaveAttribute("href", "https://example.test/runbook");
    await expect(page.getByTestId("label-badge").first()).toHaveAttribute("data-level", "internal");
    await expect(page.getByTestId("label-badge").first()).toContainText("Internal");
  });

  test("the neighbour table matches kg_context on the same store and follows the canvas", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const member = newAccount("kg-member");
    await grantOn(kg.project, member.login, "writer", "customer");
    await signInAs(member);
    // Cytoscape runs under the nonce CSP as it is: no eval, no inline script, nothing the browser refuses.
    const refused: string[] = [];
    page.on("console", (message) => {
      if (/content security policy/i.test(message.text())) refused.push(message.text());
    });
    const csp = (await page.request.get(nodePath(kg.project, SHARED_NODE))).headers()["content-security-policy"];
    expect(csp).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(cspSources(csp)).not.toContain("'unsafe-eval'"); // 'wasm-unsafe-eval' is a source of its own

    for (const hops of [1, 2] as const) {
      await open(page, nodePath(kg.project, SHARED_NODE, hops));
      await expect(page.getByTestId("kg-neighbours")).toHaveAttribute("data-selected", SHARED_NODE);
      const rows = await neighbourRows(page);
      const context = await kgContext(page.request, kg.project, [SHARED_NODE], hops);
      const expected = context.edges
        .filter((edge) => edge.src === SHARED_NODE || edge.dst === SHARED_NODE)
        .map((edge) =>
          edge.src === SHARED_NODE
            ? { node: edge.dst, rel: edge.rel, direction: "out" }
            : { node: edge.src, rel: edge.rel, direction: "in" },
        );
      const key = (row: { node: string; rel: string; direction: string }) => `${row.direction} ${row.rel} ${row.node}`;
      expect(rows.map(key).sort(), `hops=${hops}`).toEqual(expected.map(key).sort());
      expect(rows.length).toBeGreaterThan(0);
      if (hops === 1) {
        // one step out, the view is exactly kg_context's subgraph
        expect(new Set(rows.map((row) => row.node))).toEqual(
          new Set(context.nodes.map((node) => node.id).filter((id) => id !== SHARED_NODE)),
        );
      }
    }
    // The customer contract is one of KB-01's neighbours for a member whose grant reaches customer.
    expect((await neighbourRows(page)).some((row) => row.node.startsWith(CONTRACT))).toBe(true);

    // Choosing a neighbour in the table selects it in the canvas, and the table follows.
    await graphReady(page);
    const target = (await neighbourRows(page))[0].node;
    await page.getByTestId("neighbour-row").first().getByTestId("neighbour-select").click();
    await expect(page.getByTestId("kg-canvas")).toHaveAttribute("data-selected", target);
    await expect(page.getByTestId("kg-neighbours")).toHaveAttribute("data-selected", target);
    expect((await neighbourRows(page)).some((row) => row.node === SHARED_NODE)).toBe(true);
    await expect(page.getByTestId("kg-selection-announcement")).not.toBeEmpty();

    // The keyboard walks the canvas: Home returns to the node shown, arrows move the selection, the table follows.
    const canvas = page.getByTestId("kg-canvas");
    await canvas.focus();
    await page.keyboard.press("Home");
    await expect(canvas).toHaveAttribute("data-selected", SHARED_NODE);
    await page.keyboard.press("ArrowRight");
    const moved = await canvas.getAttribute("data-selected");
    expect(moved).not.toBe(SHARED_NODE);
    await expect(page.getByTestId("kg-neighbours")).toHaveAttribute("data-selected", moved!);
    await page.keyboard.press("ArrowLeft");
    await expect(canvas).toHaveAttribute("data-selected", SHARED_NODE);
    await page.getByRole("button", { name: "Vừa khung" }).click();
    await page.getByLabel("Bố cục").selectOption("cose");
    await page.getByLabel("Bố cục").selectOption("breadthfirst");
    await expect(canvas).toHaveAttribute("data-selected", SHARED_NODE);
    expect(refused).toEqual([]);
  });

  test("a node with more than 150 neighbours says the graph was cut", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const reader = newAccount("kg-reader");
    await grantOn(kg.project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, nodePath(kg.project, HUB_NODE));
    const cut = page.getByTestId("kg-truncated");
    await expect(cut).toBeVisible();
    await expect(cut).toContainText("150");
    await expect(cut).toContainText(`${NOTES} hàng xóm trực tiếp`);
    await graphReady(page);
    expect(await neighbourRows(page)).toHaveLength(149); // the node itself and 149 of its 160 neighbours
    // the full list is under the relations, by edge type
    await expect(page.getByTestId("relation-row")).toHaveCount(NOTES);
    await cut.getByRole("link").click();
    await expect(page.locator("#kg-relations")).toBeInViewport();
  });

  test("a reader whose grant stops at internal never sees a customer node", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const reader = newAccount("kg-internal");
    await grantOn(kg.project, reader.login, "reader", "internal");
    await signInAs(reader);

    await open(page, `${kgPath(kg.project)}?q=acme-contract`);
    await expect(page.getByTestId("kg-results-count")).toContainText("Không có kết quả");
    await expect(page.locator(`[data-node-id^="deals:"]`)).toHaveCount(0);
    const found = await (await page.request.get(`/v1/kg/${kg.project}/nodes?q=acme`)).json();
    expect(found.results.filter((node: { id: string }) => node.id.startsWith("deals:"))).toEqual([]);

    // By URL: the page and the API say not found, exactly as for a node that does not exist.
    await open(page, nodePath(kg.project, CONTRACT));
    await expect(page.getByTestId("state-not-found")).toContainText("Không tìm thấy node");
    expect((await page.request.get(`/v1/kg/${kg.project}/node?id=${encodeURIComponent(CONTRACT)}`)).status()).toBe(404);
    expect((await page.request.get(`/v1/kg/${kg.project}/neighbourhood?id=${encodeURIComponent(CONTRACT)}`)).status()).toBe(404);

    // KB-01's neighbourhood leaves the contract out.
    await open(page, nodePath(kg.project, SHARED_NODE));
    await expect(page.getByTestId("kg-neighbours")).toBeVisible();
    const rows = await neighbourRows(page);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.filter((row) => row.node.startsWith("deals:"))).toEqual([]);
    await expect(page.locator(`[data-node-id^="deals:"]`)).toHaveCount(0);
  });

  test("failed and queued builds show their state, times, error and the queue", async ({ page, admin, signInAs }) => {
    const { project, writer } = await kgProject(admin, "kg-status");
    const failed = await queueBuild(project, writer, true); // no knowledge config pushed: the worker fails it
    expect(failed.status).toBe("failed");
    const queued = await queueBuild(project, writer, false); // nothing takes jobs in the stack: it waits
    expect(queued.status).toBe("queued");
    const reader = newAccount("kg-reader");
    await admin.grant(project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, kgPath(project));

    const latest = page.getByTestId("kg-latest-build");
    await expect(latest).toHaveAttribute("data-build-id", String(queued.id));
    await expect(latest.getByTestId("build-status")).toHaveText("Đang chờ");
    await expect(latest.getByTestId("build-wait")).not.toHaveText("-");
    await expect(page.getByTestId("queue-job")).toHaveCount(1);
    await expect(page.getByTestId("queue-job")).toHaveAttribute("data-status", "todo");
    await expect(page.getByTestId("tile-queue")).toContainText("1 đang chờ, 0 đang chạy");
    const history = page.getByTestId("kg-build-history");
    const failedRow = history.getByRole("row").filter({ hasText: `#${failed.id}` });
    await expect(failedRow.getByTestId("build-status")).toHaveText("Thất bại");
    await expect(failedRow).toContainText("has no knowledge config on the hub yet");
    await expect(page.getByTestId("tile-graph")).toContainText("Chưa có");
    // No graph yet: search says so instead of failing.
    await expect(page.getByTestId("kg-search")).toContainText("Dự án chưa có đồ thị");
  });

  test("a rebuild of the same content reuses the artifact, and a pruned build says so", async ({ page, admin, signInAs }) => {
    const { project, writer, build: first } = await seededKg(admin);
    const same = await queueBuild(project, writer, true);
    expect(same.content_hash).toBe(first.content_hash);
    expect(same.artifact_sha256).toBe(first.artifact_sha256); // nothing uploaded
    expect(same.artifact_reused_from).toBe(first.id);
    const changed = await changeKg(project, writer, "note-extra");
    expect(changed.content_hash).not.toBe(first.content_hash);
    const pruned = await admin.pruneKg(project, 1);
    expect(pruned.projects).toEqual([expect.objectContaining({ project, artifacts: 2, kept: 1, pruned: 1, builds: 2 })]);
    expect(pruned.deleted).toBe(1);

    const reader = newAccount("kg-reader");
    await admin.grant(project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, kgPath(project));
    const latest = page.getByTestId("kg-latest-build");
    await expect(latest).toHaveAttribute("data-build-id", String(changed.id));
    await expect(latest.getByTestId("build-artifact")).toHaveAttribute("data-state", "own");
    await expect(page.getByTestId("tile-graph")).toContainText(`#${changed.id}`);
    const history = page.getByTestId("kg-build-history");
    for (const id of [first.id, same.id]) {
      const row = history.getByRole("row").filter({ has: page.getByText(`#${id}`, { exact: true }) });
      await expect(row.getByTestId("build-artifact")).toHaveAttribute("data-state", "pruned");
      await expect(row.getByTestId("build-status")).toHaveText("Thành công");
    }
    // The pages read the build that kept its artifact.
    await page.getByLabel("Từ khoá").fill("note-extra");
    await page.getByRole("button", { name: "Tìm" }).click();
    await page.waitForURL((url) => url.searchParams.get("q") === "note-extra");
    await expect(page.getByTestId("kg-result-link").first()).toHaveAttribute("data-node-id", "docs:doc:note-extra");
  });

  test.describe("on a 375 px screen", () => {
    test.use({ viewport: { width: 375, height: 812 } });

    test("the graph collapses to the neighbour table and nothing scrolls sideways", async ({ page, signInAs }) => {
      const kg = await sharedKg();
      const reader = newAccount("kg-mobile");
      await grantOn(kg.project, reader.login, "reader", "internal");
      await signInAs(reader);
      for (const path of [kgPath(kg.project), `${kgPath(kg.project)}?q=runbook`, nodePath(kg.project, SHARED_NODE)]) {
        await open(page, path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        const width = await page.evaluate(() => document.documentElement.scrollWidth);
        expect(width, path).toBeLessThanOrEqual(375);
      }
      await expect(page.getByTestId("kg-canvas")).toHaveCount(0);
      await expect(page.getByTestId("kg-neighbours")).toBeVisible();
      expect((await neighbourRows(page)).length).toBeGreaterThan(0);
      await page.getByTestId("neighbour-select").first().click();
      await expect(page.getByTestId("kg-open-selected")).toBeVisible();
    });
  });
});
