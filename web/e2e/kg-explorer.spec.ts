import { expect, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import { CONTRACT, explorerLabels, explorerReady, grantOn, kgPath, open, SHARED_NODE, sharedKg } from "./support/kg";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * The knowledge graph explorer against the graph the stack built from the synthetic fixture: the sidebar's first group
 * leads to it, it lands on the overview of the clusters, a cluster opens into its members, the search explores a
 * node, a relation and an expansion grow the view, a hidden node leaves it, and the browser's back walks the scopes.
 * A reader whose grant stops at internal finds no customer node anywhere in it.
 */
test.describe("knowledge graph explorer", () => {
  test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds a graph through the local stack");

  test("walks from the overview to a cluster and to a node, and grows the node's view", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const member = newAccount("kg-explorer");
    await grantOn(kg.project, member.login, "writer", "customer");
    await signInAs(member);
    await open(page, kgPath(kg.project));

    // The sidebar's first group holds the graph, and the explorer is the graph's first tab.
    const nav = page.getByRole("navigation", { name: "Điều hướng chính" });
    await expect(nav.getByRole("link", { name: "Đồ thị tri thức" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByTestId("kg-tab-explore")).toHaveAttribute("aria-current", "page");
    await explorerReady(page, "overview");
    const canvas = page.getByTestId("kg-explorer-canvas");
    await expect(page.getByTestId("kg-stat-nodes")).not.toBeEmpty();
    // Labels never cover each other: those without room wait for a zoom or a pointer.
    await expect.poll(() => explorerLabels(page)).toMatchObject({ overlapping: 0 });
    expect((await explorerLabels(page)).shown).toBeGreaterThan(0);

    // A cluster of the list: selected on the canvas, described in the inspector, then opened.
    const row = page.getByTestId("kg-cluster-row").first();
    const cluster = (await row.getAttribute("data-cluster-id"))!;
    await row.click();
    await expect(canvas).toHaveAttribute("data-selected", cluster);
    const inspector = page.getByTestId("kg-inspector-cluster");
    await expect(inspector).toBeVisible();
    await expect(page.getByTestId("kg-explorer-announcement")).toContainText("Đã chọn cụm");
    await inspector.getByTestId("kg-inspector-explore").click();
    await page.waitForURL((url) => url.searchParams.get("cluster") === cluster);
    await explorerReady(page, "cluster");
    await expect.poll(() => explorerLabels(page)).toMatchObject({ overlapping: 0 });
    await expect(page.getByTestId("kg-trail")).toContainText("Tổng quan");
    await page.goBack();
    await explorerReady(page, "overview");

    // The search explores a node: the node and its direct neighbours, the node in the inspector.
    const field = page.getByLabel("Tìm trong graph");
    await field.fill("KB-01");
    await field.press("Enter");
    await page.waitForURL((url) => url.searchParams.get("q") === "KB-01");
    await page.locator(`[data-testid="kg-result"][data-node-id="${SHARED_NODE}"]`).click();
    await page.waitForURL((url) => url.searchParams.get("node") === SHARED_NODE);
    await explorerReady(page, "node");
    await expect(page.getByTestId("kg-inspector-id")).toHaveText(SHARED_NODE);
    await expect(canvas).toHaveAttribute("data-selected", SHARED_NODE);
    const drawn = Number(await canvas.getAttribute("data-nodes"));
    expect(drawn).toBeGreaterThan(1);
    await expect.poll(() => explorerLabels(page)).toMatchObject({ overlapping: 0 });
    // Labels keep their size on screen when zoomed in, and still never cover each other.
    const before = await explorerLabels(page);
    expect(before.smallest).toBeGreaterThanOrEqual(10);
    await page.getByRole("button", { name: "Phóng to" }).click();
    await page.getByRole("button", { name: "Phóng to" }).click();
    await expect.poll(async () => (await explorerLabels(page)).smallest).toBe(before.smallest);
    await expect.poll(() => explorerLabels(page)).toMatchObject({ overlapping: 0 });

    // A relation selects its node; expanding it adds its own neighbours.
    const relation = page.getByTestId("kg-relation").first();
    const neighbour = (await relation.getAttribute("data-node-id"))!;
    await relation.click();
    await expect(canvas).toHaveAttribute("data-selected", neighbour);
    await expect(page.getByTestId("kg-inspector-id")).toHaveText(neighbour);
    await page.getByTestId("kg-inspector-expand").click();
    await expect(page.getByTestId("kg-expanded-row")).toHaveCount(2);
    await expect.poll(async () => Number(await canvas.getAttribute("data-nodes"))).toBeGreaterThan(drawn);

    // Hiding a node takes it out, and the rail offers it back.
    const grown = Number(await canvas.getAttribute("data-nodes"));
    await page.getByTestId("kg-inspector-hide").click();
    await expect.poll(async () => Number(await canvas.getAttribute("data-nodes"))).toBeLessThan(grown);
    await expect(page.getByTestId("kg-hidden-nodes")).toBeVisible();
    await expect(page.getByTestId("kg-expanded-row")).toHaveCount(1);

    // The keyboard: Escape clears the selection, an arrow selects a node again.
    await canvas.focus();
    await page.keyboard.press("Escape");
    await expect(canvas).toHaveAttribute("data-selected", "");
    await expect(page.getByTestId("kg-inspector-node")).toHaveCount(0);
    await page.keyboard.press("ArrowRight");
    await expect(canvas).not.toHaveAttribute("data-selected", "");

    // Back walks the scopes: the overview, with the search kept.
    await page.goBack();
    await explorerReady(page, "overview");
    await expect(page.getByTestId("kg-results")).toBeVisible();
  });

  test("a reader whose grant stops at internal never meets a customer node", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    const reader = newAccount("kg-internal");
    await grantOn(kg.project, reader.login, "reader", "internal");
    await signInAs(reader);

    await open(page, `${kgPath(kg.project)}?q=acme`);
    await expect(page.getByTestId("kg-results-count")).toBeVisible();
    await expect(page.locator(`[data-node-id^="deals:"]`)).toHaveCount(0);

    // By URL, the contract reads as a node that does not exist.
    await open(page, `${kgPath(kg.project)}?node=${encodeURIComponent(CONTRACT)}`);
    await expect(page.getByTestId("state-not-found")).toContainText("Không có node nào với id này");

    // KB-01's view leaves the contract out.
    await open(page, `${kgPath(kg.project)}?node=${encodeURIComponent(SHARED_NODE)}`);
    await explorerReady(page, "node");
    await expect(page.getByTestId("kg-inspector-relations")).toBeVisible();
    await expect(page.locator(`[data-node-id^="deals:"]`)).toHaveCount(0);
  });
});
