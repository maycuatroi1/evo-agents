import type { Page } from "@playwright/test";

import { TERMS_WARNING } from "../src/components/sharing/model";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { type Account, type HubAdmin, uniqueName } from "./support/hub";
import { unitByApi } from "./support/org";
import { claimRun, dispatch, leaseCredentials, liveWorker, seedRunPlan, startRun } from "./support/runs";
import { putSecretByApi, secretsOf, secretValue } from "./support/secrets";
import { CLEAN_REPORT, doctorReport, HIGH_REPORT, otherMember, shareByApi } from "./support/sharing";
import { toast } from "./support/toast";

/**
 * Sharing against the real API (plan credential-sharing, criterion a7): the Share dialog and the list of live shares on
 * the Accounts and Secrets pages, the terms warning of a subscription token word for word with the box that unlocks
 * Share, the cost of an account by the member each run was for, and the Sharing card of a worker with its doctor report
 * and its units. The people are made-up accounts of the fake GitHub. Pages render in English, the default.
 */
test.skip(isDeployed, "writes shares through the local stack");

const TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN";

function main(page: Page) {
  return page.locator("#main");
}

/** A run `grantee` dispatched on `project` that a worker of theirs leased: it takes `me`'s account shared with them. */
async function sharedRun(admin: HubAdmin, me: Member, grantee: Account, project: string) {
  await admin.grant(project, grantee.login, "writer", "internal");
  await seedRunPlan(me, project);
  const live = await liveWorker(grantee, project, uniqueName("desk"));
  const [run] = await dispatch(grantee, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  const credentials = await leaseCredentials(live, run.id);
  return credentials.leases.find((item) => item.env_var === TOKEN_VAR);
}

test("a subscription token is shared only once its owner ticks the warning, which shows word for word", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const lan = await otherMember("lan-tran");
  await putSecretByApi(me, "claude-main", { kind: "env", env_var: TOKEN_VAR, projects: [me.projects[0]], value: secretValue("oauth") });

  await page.goto("/accounts?tab=sharing");
  const shares = main(page).getByTestId("account-shares");
  await expect(shares.getByRole("heading", { name: "Shared with other members" })).toBeVisible();
  await expect(shares.getByTestId("shares-empty")).toHaveText("Nothing is shared.");
  await expect(main(page).getByTestId("accounts-cost-by-principal")).toHaveCount(0);

  await shares.getByTestId("share-open").click();
  const dialog = page.getByTestId("share-dialog");
  await expect(dialog.getByRole("heading", { name: "Share a credential" })).toBeVisible();
  await expect(dialog.getByTestId("share-secret")).toHaveValue("claude-main");
  await expect(dialog.getByTestId("share-terms-text")).toHaveText(TERMS_WARNING);
  const save = dialog.getByTestId("share-save");
  await expect(save).toBeDisabled();
  await expectNoSeriousViolations(page, "share dialog with the terms warning");

  // Everything by keyboard: the member, then the box under the warning, which unlocks Share.
  await dialog.getByTestId("share-login").fill(lan.login);
  await dialog.getByTestId("share-ceiling").fill("80");
  await dialog.getByTestId("share-maxRunning").fill("2");
  await dialog.getByTestId("share-terms-ack").focus();
  await page.keyboard.press("Space");
  await expect(dialog.getByTestId("share-terms-ack")).toBeChecked();
  await expect(save).toBeEnabled();
  await save.focus();
  await page.keyboard.press("Enter");
  await expect(toast(page, "claude-main is shared")).toBeVisible();
  await expect(dialog).toBeHidden();

  const row = shares.getByTestId("share-row");
  await expect(row).toHaveCount(1);
  await expect(row.getByTestId("share-grantee")).toHaveText(lan.login);
  await expect(row.getByTestId("share-limits")).toHaveText("80% ceiling, 2 runs at once");
  await expect(row.getByTestId("share-ends-soon")).toHaveCount(0);
  await expect(row.getByTestId("share-over-ceiling")).toHaveCount(0);
  const [stored] = (await secretsOf(me)).filter((item) => item.name === "claude-main");
  expect(stored.shares.map((share) => [share.grantee, share.terms_ack_at !== null])).toEqual([[lan.login, true]]);

  // Shared and not yet used: the cost by member says so.
  await expect(main(page).getByTestId("accounts-cost-empty")).toHaveText("No run of another member has used them yet.");

  await row.getByTestId("share-revoke").click();
  const confirm = page.getByTestId("share-revoke-dialog");
  await expect(confirm.getByRole("heading", { name: "Revoke the share of claude-main?" })).toBeVisible();
  await page.getByTestId("share-revoke-dialog-confirm").click();
  await expect(toast(page, "Share of claude-main revoked")).toBeVisible();
  await expect(shares.getByTestId("shares-empty")).toBeVisible();
});

test("a subscription token on the Secrets page asks for the warning too", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const minh = await otherMember("minh-le");
  await putSecretByApi(me, "team-token", { kind: "env", env_var: TOKEN_VAR, projects: [me.projects[0]], value: secretValue("oauth") });

  await page.goto("/secrets");
  const shares = main(page).getByTestId("secret-shares");
  await shares.getByTestId("share-open").click();
  const dialog = page.getByTestId("share-dialog");
  await expect(dialog.getByTestId("share-terms-text")).toHaveText(TERMS_WARNING);
  await dialog.getByTestId("share-login").fill(minh.login);
  await dialog.getByTestId("share-terms-ack").check();
  await dialog.getByTestId("share-save").click();
  await expect(shares.getByTestId("share-row").getByTestId("share-grantee")).toHaveText(minh.login);
});

test("an API key is shared with a unit on the Secrets page without the warning, and a share near its end says so", async ({
  page,
  member,
}) => {
  const me = await member([
    { role: "writer", maxLevel: "internal" },
    { role: "writer", maxLevel: "internal" },
  ]);
  const unit = await unitByApi("team-a");
  await putSecretByApi(me, "search-key", { kind: "env", env_var: "SEARCH_API_KEY", projects: me.projects, value: secretValue("search") });

  await page.goto("/secrets");
  const shares = main(page).getByTestId("secret-shares");
  await shares.getByTestId("share-open").click();
  const dialog = page.getByTestId("share-dialog");
  await expect(dialog.getByTestId("share-terms")).toHaveCount(0);

  // What the hub would refuse is said under its field before anything is sent.
  await dialog.getByTestId("share-kind").selectOption("unit");
  await dialog.getByTestId("share-budget").fill("0");
  await dialog.getByTestId("share-save").click();
  await expect(dialog.getByTestId("share-unit-error")).toHaveText("Fill this in.");
  await expect(dialog.getByTestId("share-budget-error")).toHaveText(/^Enter a number from 0\.01 to 1,?000,?000\.$/);

  await dialog.getByTestId("share-unit").selectOption(unit);
  await dialog.getByTestId("share-budget").fill("25");
  await dialog.getByTestId("share-level").selectOption("A2");
  await dialog.getByTestId("share-projects").getByRole("checkbox", { name: me.projects[1] }).uncheck();
  await dialog.getByTestId("share-expires").fill(new Date(Date.now() + 3 * 86_400_000).toISOString().slice(0, 10));
  await dialog.getByTestId("share-save").click();
  await expect(toast(page, "search-key is shared")).toBeVisible();

  const row = shares.getByTestId("share-row");
  await expect(row.getByTestId("share-grantee")).toHaveText(`unit ${unit}`);
  await expect(row.getByTestId("share-grantee")).toHaveAttribute("data-kind", "unit");
  await expect(row.getByTestId("share-limits")).toHaveText("up to A2, $25.00 budget");
  await expect(row.getByTestId("share-ends-soon")).toHaveText("Ends soon");
  await expect(row).toContainText(me.projects[0]);
  await expect(row).not.toContainText(me.projects[1]);
});

test("a member the hub does not know is refused with the hub's reason", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  await putSecretByApi(me, "search-key", { kind: "env", env_var: "SEARCH_API_KEY", projects: [me.projects[0]], value: secretValue("search") });
  await page.goto("/secrets");
  await main(page).getByTestId("secret-shares").getByTestId("share-open").click();
  const dialog = page.getByTestId("share-dialog");
  await dialog.getByTestId("share-login").fill(uniqueName("nobody"));
  await dialog.getByTestId("share-save").click();
  await expect(dialog.getByRole("alert")).toContainText("no member of this hub has the login");
  await expect(dialog).toBeVisible();
});

test("the owner of a shared account reads what each member's runs took of it", async ({ page, member, admin }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const lan = await otherMember("lan-tran");
  await putSecretByApi(me, "claude-main", { kind: "env", env_var: TOKEN_VAR, projects: [project], value: secretValue("oauth") });
  await shareByApi(me, "claude-main", { grantee_kind: "user", login: lan.login, projects: [project], terms_ack: true });
  const lease = await sharedRun(admin, me, lan, project);
  expect(lease?.name).toBe("claude-main");

  await page.goto("/accounts?tab=sharing");
  const table = main(page).getByTestId("cost-by-principal-table");
  await expect(table.getByTestId("cost-principal")).toHaveText([lan.login]);
  await expect(table.getByTestId("cost-month")).toHaveText(["$0.00"]);
  await expect(table).toContainText("claude-main");
});

test("a worker narrows to a unit once its doctor is fresh and clean, and a high finding locks it with the reason", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const live = await liveWorker(me, me.projects[0], uniqueName("studio"));
  const unit = await unitByApi("team-b");

  await page.goto(`/workers/${live.worker.id}`);
  const card = main(page).getByTestId("worker-sharing");
  await expect(card.getByRole("heading", { name: "Sharing" })).toBeVisible();
  await expect(card.getByTestId("worker-doctor")).toHaveAttribute("data-state", "none");
  await expect(card.getByTestId("worker-doctor-none")).toHaveText("None yet");
  await expect(card.getByTestId("worker-units-empty")).toBeVisible();
  await expect(card.getByTestId("worker-sharing-blocked")).toContainText("no doctor report");
  await expect(card.getByTestId("worker-unit-select")).toBeDisabled();
  await expect(card.getByTestId("worker-unit-add")).toBeDisabled();

  await doctorReport(live, CLEAN_REPORT);
  await page.reload();
  await expect(card.getByTestId("worker-doctor")).toHaveAttribute("data-state", "ok");
  await expect(card.getByTestId("worker-doctor-counts")).toHaveText("0 high, 1 medium");
  await expect(card.getByTestId("worker-doctor-high")).toHaveCount(0);
  await expect(card.getByTestId("worker-sharing-blocked")).toHaveCount(0);
  await expectNoSeriousViolations(page, "worker sharing card");

  // By keyboard: pick the unit, then Enter on Add.
  await card.getByTestId("worker-unit-select").selectOption(unit);
  await card.getByTestId("worker-unit-add").focus();
  await page.keyboard.press("Enter");
  await expect(toast(page, `${live.worker.name} is shared with unit ${unit}`)).toBeVisible();
  await expect(card.locator(`[data-unit="${unit}"]`)).toContainText(`Unit ${unit}`);

  await doctorReport(live, HIGH_REPORT);
  await page.reload();
  await expect(card.getByTestId("worker-doctor-high")).toHaveText("1 high");
  await expect(card.getByTestId("worker-sharing-blocked")).toContainText("1 high finding");
  // A shared worker says why next to its switch and may still stop; stopped, it cannot be shared again until then.
  const toggle = card.getByTestId("worker-shared-switch");
  await expect(card.getByTestId("worker-shared-locked")).toContainText("1 high finding");
  await expect(toggle).toBeChecked();
  await toggle.click();
  await expect(toast(page, `${live.worker.name} takes your runs alone`)).toBeVisible();
  await expect(toggle).not.toBeChecked();
  await expect(toggle).toBeDisabled();
  await expect(card.getByTestId("worker-shared")).toHaveAttribute("data-locked", "true");

  await card.locator(`[data-unit="${unit}"]`).getByTestId("worker-unit-remove").click();
  await expect(toast(page, `Unit ${unit} taken back from ${live.worker.name}`)).toBeVisible();
  await expect(card.getByTestId("worker-units-empty")).toBeVisible();
  await expect(toggle).toBeEnabled();
  await expect(card.getByTestId("worker-shared-locked")).toHaveCount(0);
});
