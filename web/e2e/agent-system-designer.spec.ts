import type { Locator, Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, uniqueName } from "./support/hub";
import { revealNavItem } from "./support/nav";
import { open } from "./support/plans";
import { bumpAgentByApi, chainSpec, putSystemByApi, systemOf, systemPath, systemsPath } from "./support/systems";

/**
 * The designer of agent systems against the real API (web/DESIGN.md, Agent systems): an admin creates a system from
 * the project's nav, adds agents from the palette, joins them by dragging from a handle or from the side panel,
 * describes an edge and saves a new revision; the hub's problems show on the node or edge they name and hold Save;
 * a pin behind its profile offers Upgrade; the config panel writes a new profile version and pins it; a revision saved
 * elsewhere meanwhile shows the conflict with the way to the latest; Test opens the chat with the entry agent; and a
 * writer only reads. Pages render in English.
 */
test.skip(isDeployed, "creates agents and systems through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function node(page: Page, id: string): Locator {
  return main(page).locator(`[data-testid="system-node"][data-node="${id}"]`);
}

function edges(page: Page, kind?: string): Locator {
  return main(page).locator(kind ? `[data-testid="system-edge"][data-kind="${kind}"]` : '[data-testid="system-edge"]');
}

async function designerAt(page: Page, revision: number) {
  await expect(main(page).getByTestId("system-designer")).toHaveAttribute("data-revision", String(revision));
}

async function save(page: Page, revision: number) {
  await main(page).getByTestId("system-save").click();
  await designerAt(page, revision);
  await expect(main(page).getByTestId("system-state")).toHaveAttribute("data-dirty", "false");
}

async function addAgent(page: Page, handle: string) {
  await main(page).getByTestId("system-add").click();
  await page.getByTestId("system-add-agent").click();
  const dialog = page.getByTestId("system-add-dialog");
  await dialog.getByTestId("system-add-profile").selectOption(handle);
  await dialog.getByTestId("system-add-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(node(page, handle)).toBeVisible();
}

test("an admin creates a system from the nav at once, renames it, adds two agents, joins and describes them, and saves revision 3", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const triage = uniqueName("triage");
  const billing = uniqueName("billing");
  await createAgentByApi(me, project, { handle: triage, name: "Triage", permissions: {} });
  await createAgentByApi(me, project, { handle: billing, name: "Billing", permissions: {} });

  await open(page, `/p/${project}`);
  await revealNavItem(page, "nav-systems");
  await page.getByTestId("nav-systems").click();
  await page.waitForURL(`**${systemsPath(project)}`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Agent systems" })).toBeVisible();
  await expect(page.getByTestId("nav-systems")).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("systems-empty")).toBeVisible();

  // New system creates one at once, with no form first: System 1 at system-1, in its designer.
  await main(page).getByTestId("systems-new").first().click();
  await page.waitForURL(`**${systemPath(project, "system-1")}`);
  await designerAt(page, 1);
  await expect(main(page).getByTestId("system-name")).toHaveText("System 1");
  await expect(node(page, "entry")).toBeVisible();
  // Test needs a saved entry agent.
  await expect(main(page).getByTestId("system-test")).toBeDisabled();

  // The first agent joins the entry by itself; the second one by the side panel's Connect to.
  await addAgent(page, triage);
  await expect(edges(page, "interacts")).toHaveCount(1);
  await addAgent(page, billing);
  await expect(main(page).getByTestId("system-node-panel")).toHaveAttribute("data-node", billing);
  await node(page, triage).click();
  await main(page).getByTestId("system-node-connect").selectOption(billing);
  await expect(edges(page, "interacts")).toHaveCount(2);
  const description = main(page).getByTestId("system-edge-description");
  await description.fill("Hands over refunds and invoices.");
  await description.blur();
  await expect(main(page).getByTestId("system-state")).toHaveAttribute("data-dirty", "true");

  // A rename from the title while the draft has changes: saved at once as revision 2, and the draft follows it.
  await main(page).getByTestId("system-name").click();
  const title = main(page).getByTestId("system-name-input");
  await expect(title).toHaveValue("System 1");
  await title.fill("Support desk");
  await title.press("Enter");
  await expect(main(page).getByTestId("system-name")).toHaveText("Support desk");
  await expect(main(page).getByTestId("system-revision")).toHaveText("Revision 2");
  await designerAt(page, 2);
  await expect(main(page).getByTestId("system-state")).toHaveAttribute("data-dirty", "true");
  // Escape leaves the name as it is.
  await main(page).getByTestId("system-name").click();
  await title.fill("Nothing");
  await title.press("Escape");
  await expect(main(page).getByTestId("system-name")).toHaveText("Support desk");

  // Undo takes the description back, redo brings it again.
  await main(page).getByTestId("system-undo").click();
  await expect(edges(page, "interacts")).toHaveCount(2);
  await main(page).getByTestId("system-redo").click();
  await main(page).getByTestId("system-layout").click();
  await save(page, 3);

  const saved = await systemOf(me, project, "system-1");
  expect(saved.revision).toBe(3);
  expect(saved.spec.name).toBe("Support desk");
  expect(saved.entry_agent).toBe(triage);
  expect(saved.spec.edges).toContainEqual({ from: triage, to: billing, kind: "interacts", description: "Hands over refunds and invoices." });

  // The list reads it back.
  await open(page, systemsPath(project));
  const row = main(page).getByTestId("systems-table").locator("tbody tr");
  await expect(row).toHaveCount(1);
  await expect(row.first()).toContainText("Support desk");
  await expect(row.first()).toContainText(`@${triage}`);
});

test("dragging from an agent's handle offers only the nodes an edge fits, and drops the one edge that does", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const front = uniqueName("front");
  const back = uniqueName("back");
  await createAgentByApi(me, project, { handle: front, name: "Front", permissions: {} });
  const second = await createAgentByApi(me, project, { handle: back, name: "Back", permissions: {} });
  const spec = chainSpec("Drag", [{ handle: front, version: 1 }]);
  spec.nodes.push({ id: back, kind: "agent", profile: back, version: second.version, position: { x: 300, y: 260 } });
  spec.nodes.push({ id: "curator", kind: "curator", position: { x: 0, y: 260 } });
  await putSystemByApi(me, project, "drag", spec);
  await open(page, systemPath(project, "drag"));
  await designerAt(page, 1);

  const handle = main(page).locator(`.react-flow__handle.source[data-nodeid="${front}"]`);
  const from = await handle.boundingBox();
  const to = await node(page, back).boundingBox();
  if (!from || !to) throw new Error("the canvas did not lay out");
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 });
  // While dragging, the other agent offers Talks to; the curator offers the edge that fits the other way (it optimizes
  // the agent); the entry, which already talks to this agent and talks to one alone, offers none.
  await expect(node(page, back)).toHaveAttribute("data-drop", "interacts");
  await expect(node(page, "curator")).toHaveAttribute("data-drop", "optimizes");
  await expect(node(page, "entry")).toHaveAttribute("data-drop", "none");
  await page.mouse.up();
  await expect(edges(page, "interacts")).toHaveCount(2);
  await expect(main(page).getByTestId("system-edge-panel")).toBeVisible();
});

test("the hub's problems show on their edge and hold Save, until undo takes the change back", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const solo = uniqueName("solo");
  await createAgentByApi(me, project, { handle: solo, name: "Solo", permissions: {} });
  await putSystemByApi(me, project, "checks", chainSpec("Checks", [{ handle: solo, version: 1 }]));
  await open(page, systemPath(project, "checks"));
  await designerAt(page, 1);

  await edges(page, "interacts").first().click();
  const description = main(page).getByTestId("system-edge-description");
  await description.fill("x".repeat(501));
  await description.blur();
  await expect(edges(page, "interacts").first()).toHaveAttribute("data-level", "error");
  await expect(main(page).getByTestId("system-edge-panel").getByTestId("system-problem")).toHaveAttribute("data-level", "error");
  await expect(main(page).getByTestId("system-save")).toBeDisabled();

  await main(page).getByTestId("system-undo").click();
  await expect(edges(page, "interacts").first()).toHaveAttribute("data-level", "");
  await expect(main(page).getByTestId("system-state")).toHaveAttribute("data-dirty", "false");
});

test("a pin behind its profile offers Upgrade, which pins the newer version in a new revision", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const helper = uniqueName("helper");
  await createAgentByApi(me, project, { handle: helper, name: "Helper", permissions: {} });
  await putSystemByApi(me, project, "pins", chainSpec("Pins", [{ handle: helper, version: 1 }]));
  const bumped = await bumpAgentByApi(me, project, helper, "Answers first.");
  await open(page, systemPath(project, "pins"));
  await designerAt(page, 1);

  const upgrade = node(page, helper).getByTestId("system-node-upgrade");
  await expect(upgrade).toContainText(`v${bumped.version}`);
  await upgrade.click();
  await designerAt(page, 2);
  await expect(upgrade).toBeHidden();
  const saved = await systemOf(me, project, "pins");
  expect(saved.spec.nodes).toContainEqual(expect.objectContaining({ id: helper, version: bumped.version }));
});

test("the config panel saves a new profile version and pins it; Test opens the chat with the entry agent", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const desk = uniqueName("desk");
  await createAgentByApi(me, project, { handle: desk, name: "Desk", permissions: {} });
  await putSystemByApi(me, project, "config", chainSpec("Config", [{ handle: desk, version: 1 }]));
  await open(page, systemPath(project, "config"));
  await designerAt(page, 1);

  await node(page, desk).click();
  const config = main(page).getByTestId("agent-config");
  await config.getByTestId("agent-config-description-input").fill("Greets and routes.");
  await config.getByTestId("agent-config-runs-on").getByRole("button", { name: "Hub" }).click();
  await config.getByTestId("agent-config-save").click();
  await designerAt(page, 2);
  const saved = await systemOf(me, project, "config");
  expect(saved.spec.nodes).toContainEqual(expect.objectContaining({ id: desk, version: 2 }));

  await main(page).getByTestId("system-test").click();
  const sheet = page.getByTestId("system-test-sheet");
  await expect(sheet.getByTestId("system-test-chat").getByTestId("agent-chat")).toHaveAttribute("data-agent", desk);
  await expect(sheet.getByTestId("chat-home-composer")).toBeVisible();
  await expect(sheet.getByTestId("system-test-waiting")).toBeVisible();
});

test("a revision saved elsewhere while editing shows the conflict, and Load latest brings it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const lead = uniqueName("lead");
  await createAgentByApi(me, project, { handle: lead, name: "Lead", permissions: {} });
  const spec = chainSpec("Race", [{ handle: lead, version: 1 }]);
  await putSystemByApi(me, project, "race", spec);
  await open(page, systemPath(project, "race"));
  await designerAt(page, 1);

  // Someone else saves revision 2 meanwhile.
  await putSystemByApi(me, project, "race", { ...spec, purpose: "Answers the front desk." }, 1);

  await edges(page, "interacts").first().click();
  const description = main(page).getByTestId("system-edge-description");
  await description.fill("Takes every message first.");
  await description.blur();
  await main(page).getByTestId("system-save").click();
  const banner = main(page).getByTestId("system-conflict");
  await expect(banner).toContainText("Revision 2 was saved while you edited");
  await expect(main(page).getByTestId("system-save")).toBeDisabled();

  await banner.getByTestId("system-conflict-load").click();
  await designerAt(page, 2);
  await expect(banner).toBeHidden();
  await expect(main(page).getByTestId("system-state")).toHaveAttribute("data-dirty", "false");
});

test("a writer reads a system without the tools that change it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const reader = uniqueName("reader");
  await createAgentByApi(me, project, { handle: reader, name: "Reader", permissions: {} });
  await putSystemByApi(me, project, "read", chainSpec("Read", [{ handle: reader, version: 1 }]));
  await new HubAdmin().grant(project, me.login, "writer", "internal");
  await open(page, systemPath(project, "read"));
  await expect(main(page).getByTestId("system-read-only")).toBeVisible();
  await expect(main(page).getByTestId("system-add")).toHaveCount(0);
  await expect(main(page).getByTestId("system-save")).toHaveCount(0);
  await expect(node(page, reader)).toBeVisible();
});
