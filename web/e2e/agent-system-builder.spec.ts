import type { Locator, Page } from "@playwright/test";

import { createAgentByApi, agentOf } from "./support/agents";
import { BUILDER, builderProposes, builderStarts, enableBuilder } from "./support/builder";
import { chatPageOf, openDirectByApi } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { chainSpec, putSystemByApi, type SystemSpec, systemOf, systemPath } from "./support/systems";
import { toast } from "./support/toast";

/**
 * @builder designs an agent system from a member's words (plan multi-agent-systems, criterion a11; docs/hub.md,
 * Designing a system with @builder): the designer's Builder panel is the chat with @builder (the chat home's frame);
 * what it proposes, a spec evo-agent-system/1 with the profiles it needs, shows on the canvas as faded, dashed nodes and
 * edges over the saved revision. Discard changes nothing; Accept, an admin's, makes the profile and writes the next
 * revision through @builder's approval. The spec plays @builder's worker; no model is asked anything.
 */
test.skip(isDeployed, "runs a fake worker of @builder against the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function node(page: Page, id: string): Locator {
  return main(page).locator(`[data-testid="system-node"][data-node="${id}"]`);
}

async function designerAt(page: Page, revision: number) {
  await expect(main(page).getByTestId("system-designer")).toHaveAttribute("data-revision", String(revision));
}

/** `spec` with an agent node of profile `handle` after `after`, which talks to it. */
function withReviewer(spec: SystemSpec, after: string, handle: string): SystemSpec {
  return {
    ...spec,
    nodes: [...spec.nodes, { id: handle, kind: "agent", profile: handle, label: "Reviewer", position: { x: 600, y: 0 } }],
    edges: [...spec.edges, { from: after, to: handle, kind: "interacts", description: "a second look" }],
  };
}

test("an admin asks @builder in the designer, sees its proposal faded on the canvas, discards one and accepts the next", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await enableBuilder(me, project);
  const triage = uniqueName("triage");
  const made = await createAgentByApi(me, project, { handle: triage, name: "Triage", permissions: {} });
  const slug = "support-desk";
  await putSystemByApi(me, project, slug, chainSpec("Northwind support", [{ handle: triage, version: made.version }]));

  await open(page, systemPath(project, slug));
  await designerAt(page, 1);
  await main(page).getByTestId("system-builder").click();
  const panel = main(page).getByTestId("system-builder-panel");
  await expect(panel.getByTestId("system-builder-chat").getByTestId("agent-chat")).toHaveAttribute("data-agent", BUILDER);
  await expect(panel.getByTestId("system-builder-proposal")).toHaveCount(0);

  // The member's words go to @builder as a person's would; its worker takes the chat run.
  await panel.getByTestId("chat-home-composer-text").fill(`Add a reviewer after triage in ${slug}`);
  await panel.getByTestId("chat-home-composer-send").click();
  await expect(panel.getByTestId("chat-view")).toBeVisible();
  const chat = await openDirectByApi(me, BUILDER);
  const [asked] = (await chatPageOf(me, chat.id)).messages;
  const runId = asked.mentions[0]?.run_id;
  expect(runId, "the message hands @builder a run").toBeTruthy();
  await builderStarts(live, runId!);

  // @builder proposes a reviewer and the profile it needs: the canvas draws them faded over revision 1.
  const held = (await systemOf(me, project, slug)).spec as unknown as SystemSpec;
  const reviewer = uniqueName("reviewer");
  const change = {
    spec: withReviewer(held, triage, reviewer),
    agents: [{ handle: reviewer, name: "Reviewer", instructions: "Read the answer of triage again before it goes out." }],
  };
  const first = await builderProposes(live, runId!, { kind: "system", summary: "A reviewer after triage", target: { system: slug }, set: change });
  const card = panel.getByTestId("system-builder-proposal");
  await expect(card).toHaveAttribute("data-proposal", String(first));
  await expect(card).toContainText("A reviewer after triage");
  await expect(card.getByTestId("system-builder-counts")).toHaveText("1 node added, 0 nodes dropped, 0 changes, 1 edge added, 0 edges dropped");
  await expect(card).toContainText(`New agent @${reviewer} (Reviewer)`);
  await expect(main(page).getByTestId("system-canvas")).toHaveAttribute("data-preview", "true");
  await expect(node(page, reviewer)).toHaveAttribute("data-ghost", "added");
  await expect(node(page, triage)).toHaveAttribute("data-ghost", "");
  await expect(main(page).locator('[data-testid="system-edge"][data-ghost="added"]')).toHaveCount(1);

  // Show the saved revision hides the proposal; Show on canvas draws it again.
  await card.getByTestId("system-builder-show").click();
  await expect(node(page, reviewer)).toHaveCount(0);
  await card.getByTestId("system-builder-show").click();
  await expect(node(page, reviewer)).toHaveAttribute("data-ghost", "added");

  // Discard changes nothing: no revision, no profile, the canvas back to revision 1.
  await card.getByTestId("system-builder-discard").click();
  await expect(toast(page, `Proposal ${first} discarded; nothing changed`)).toBeVisible();
  await expect(card).toHaveCount(0);
  await expect(node(page, reviewer)).toHaveCount(0);
  expect((await systemOf(me, project, slug)).revision).toBe(1);
  await expect(agentOf(me, project, reviewer)).rejects.toThrow();

  // The next proposal, accepted: the profile is made and the spec is revision 2, drawn as saved.
  const second = await builderProposes(live, runId!, { kind: "system", summary: "A reviewer after triage, again", target: { system: slug }, set: change });
  await expect(card).toHaveAttribute("data-proposal", String(second));
  await expect(node(page, reviewer)).toHaveAttribute("data-ghost", "added");
  await card.getByTestId("system-builder-accept").click();
  await expect(toast(page, "Saved as revision 2")).toBeVisible();
  await designerAt(page, 2);
  await expect(card).toHaveCount(0);
  await expect(node(page, reviewer)).toHaveAttribute("data-ghost", "");
  const saved = await systemOf(me, project, slug);
  expect(saved.revision).toBe(2);
  expect((saved.spec as unknown as SystemSpec).nodes.map((one) => one.id)).toContain(reviewer);
  expect((await agentOf(me, project, reviewer)).name).toBe("Reviewer");
});
