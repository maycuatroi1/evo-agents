import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { dutyWorker, writeCharter } from "./support/curator";
import { STACK_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";

/**
 * The Curator's morning brief on the web: at the charter's brief_at the hub sends the owner of the night shift's
 * schedule a notice (hub_stack POST /curator/brief plays the job curator.brief at that moment), which the Inbox lists
 * as a Morning brief with the night's figures, and the Curator's page names the last brief with a link to it. axe
 * checks both.
 */
test.skip(isDeployed, "sends a brief through the local stack");

const two = (n: number) => String(n).padStart(2, "0");

async function sendBrief(project: string, at: Date): Promise<string[]> {
  const response = await fetch(`${STACK_URL}/curator/brief`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project, at: at.toISOString() }),
  });
  if (!response.ok) throw new Error(`hub_stack /curator/brief: ${response.status} ${await response.text()}`);
  return ((await response.json()) as { outcomes: string[] }).outcomes;
}

function main(page: Page) {
  return page.locator("#main");
}

test("the morning brief reaches the owner's Inbox and the Curator's page names it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await dutyWorker(me, project, uniqueName("night"));
  const now = new Date();
  await writeCharter(me, project, live.worker.name, { brief_at: `${two(now.getUTCHours())}:${two(now.getUTCMinutes())}` });
  expect(await sendBrief(project, new Date())).toEqual(["brief"]);
  expect(await sendBrief(project, new Date())).toEqual(["sent"]); // once a day

  await open(page, `/inbox?kind=notice&project=${project}`);
  const item = main(page).locator('[data-testid="notification"][data-notice-kind="curator_brief"]');
  await expect(item).toHaveCount(1);
  await expect(item.getByTestId("notice-kind")).toHaveText("Morning brief");
  await expect(item).toContainText(`Morning brief of ${project}: 0 runs, $0.00 of $2.00`);
  await expect(item.getByTestId("notification-body")).toContainText(`Worker ${live.worker.name}: online`);
  await expectNoSeriousViolations(page, "the Inbox with a morning brief");

  await open(page, `/p/${project}/curator`);
  const fact = main(page).getByTestId("curator-last-brief");
  await expect(fact).toContainText(`to ${me.login}`);
  await expect(fact.getByTestId("curator-last-brief-link")).toHaveAttribute("href", `/inbox?kind=notice&project=${project}`);
  await expectNoSeriousViolations(page, "the Curator's page with its last brief");
});
