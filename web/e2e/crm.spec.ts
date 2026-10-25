import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { CONTACTS, contactPath, contactsPath, seedGrowth } from "./support/growth";
import { HubAdmin, newAccount } from "./support/hub";

/**
 * Criterion a17 of plan autonomous-oss-growth for the core plugin crm, in the browser against the real API: the
 * Contacts pages exist only where a project enabled crm; the pipeline counts each stage and filters the list; a
 * contact's page shows its touchpoints and the stages it reached; a writer adds a note; an admin deletes a contact
 * behind a confirmation; a reader only reads.
 */
test.skip(isDeployed, "enables plugins and seeds contacts through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The crm page the hub frames in the main column (web/DESIGN.md, Plugins). */
function frame(page: Page) {
  return main(page).frameLocator('[data-testid="plugin-frame"]');
}

test("Contacts show only where a project enabled crm, then the pipeline counts each stage and filters the list", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await page.goto(contactsPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-crm-contacts")).toHaveCount(0);

  await seedGrowth(me, project);
  await page.goto(`/p/${project}/x/crm`);
  await expect(page).toHaveURL(contactsPath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
  await expect(frame(page).getByTestId("crm-contact-row")).toHaveCount(4);
  await expect(frame(page).getByTestId("crm-stage-all").getByTestId("crm-stage-count")).toHaveText("4");
  for (const stage of ["lead", "invited", "activated", "retained"]) {
    await expect(frame(page).getByTestId(`crm-stage-${stage}`).getByTestId("crm-stage-count")).toHaveText("1");
  }

  await frame(page).getByTestId("crm-stage-activated").click();
  await expect(frame(page).getByTestId("crm-stage-activated")).toHaveAttribute("aria-pressed", "true");
  const rows = frame(page).getByTestId("crm-contact-row");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText(CONTACTS.activated);
  await expect(rows).toContainText("Invite link");

  await rows.getByTestId("crm-contact-link").click();
  await expect(page).toHaveURL(contactPath(project, CONTACTS.activated));
  await expect(frame(page).getByTestId("crm-history-step")).toHaveCount(3);
  await expect(frame(page).getByTestId("crm-touchpoint")).toHaveCount(1);
  await expect(frame(page).getByTestId("crm-profile")).toHaveAttribute("href", `https://github.com/${CONTACTS.activated}`);
  await frame(page).getByTestId("crm-back").click();
  await expect(page).toHaveURL(contactsPath(project));
});

test("a writer adds a note to a contact, newest first, and has no Delete", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const [project] = me.projects;
  const owner = newAccount("crm-owner");
  await new HubAdmin().grant(project, owner.login, "admin", "internal");
  await seedGrowth(owner, project);
  await page.goto(contactPath(project, CONTACTS.invited));
  await expect(frame(page).getByTestId("crm-touchpoint")).toHaveCount(1);
  await expect(frame(page).getByTestId("crm-delete")).toHaveCount(0);

  await expect(frame(page).getByTestId("crm-note-save")).toBeDisabled();
  await frame(page).getByTestId("crm-note").fill("Met at the Hanoi meetup; wants the worker on Windows.");
  await frame(page).getByTestId("crm-note-save").click();
  const points = frame(page).getByTestId("crm-touchpoint");
  await expect(points).toHaveCount(2);
  await expect(points.first()).toContainText("Met at the Hanoi meetup");
  await expect(points.first()).toContainText(me.login);
  await expect(frame(page).getByTestId("crm-note")).toHaveValue("");
});

test("an admin deletes a contact only after confirming, and the list no longer has it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedGrowth(me, project);
  await page.goto(contactPath(project, CONTACTS.retained));

  await frame(page).getByTestId("crm-delete").click();
  const dialog = frame(page).getByRole("alertdialog");
  await expect(dialog).toContainText(`Delete ${CONTACTS.retained}?`);
  await dialog.getByRole("button", { name: "Keep" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(contactPath(project, CONTACTS.retained));

  await frame(page).getByTestId("crm-delete").click();
  await frame(page).getByTestId("crm-delete-confirm-confirm").click();
  await expect(page).toHaveURL(contactsPath(project));
  await expect(frame(page).getByTestId("crm-contact-row")).toHaveCount(3);
  await expect(frame(page).getByTestId("crm-contacts")).not.toContainText(CONTACTS.retained);
  await expect(frame(page).getByTestId("crm-stage-retained").getByTestId("crm-stage-count")).toHaveText("0");
});

test("a reader reads the contacts and a contact's page, with no note and no Delete", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const [project] = me.projects;
  const owner = newAccount("crm-owner");
  await new HubAdmin().grant(project, owner.login, "admin", "internal");
  await seedGrowth(owner, project);
  await page.goto(contactsPath(project));
  await expect(frame(page).getByTestId("crm-read-only")).toBeVisible();
  await expect(frame(page).getByTestId("crm-contact-row")).toHaveCount(4);
  await page.goto(contactPath(project, CONTACTS.lead));
  await expect(frame(page).getByTestId("crm-touchpoint")).toHaveCount(1);
  await expect(frame(page).getByTestId("crm-read-only")).toBeVisible();
  await expect(frame(page).getByTestId("crm-note-form")).toHaveCount(0);
  await expect(frame(page).getByTestId("crm-delete")).toHaveCount(0);
});
