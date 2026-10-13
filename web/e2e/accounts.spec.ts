import type { Page } from "@playwright/test";

import { API_URL } from "./support/env";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { claimRun, dispatch, leaseCredentials, type LiveWorker, liveWorker, runPath, seedRunPlan, startRun } from "./support/runs";
import { putSecretByApi, recordResponses, secretValue } from "./support/secrets";
import { toast } from "./support/toast";
import { PROTOCOL_HEADER } from "./support/workers";

/**
 * The Accounts page and the accounts of a run against the real API (docs/credentials.md, section "Accounts"): a
 * member's env secrets for the same variable, each with its state, its last usage, its runs and their cost, and the
 * runs holding it; Pause, Resume and Edit on each, and the rotation policy of each project; the run page names the
 * account the run took and each usage limit it hit. No value is ever sent to the browser. Pages render in English.
 */
test.skip(isDeployed, "writes accounts through the local stack");

const VAR = "CLAUDE_CODE_OAUTH_TOKEN";
const RESET = "2031-05-01T12:00:00Z";

function main(page: Page) {
  return page.locator("#main");
}

function row(page: Page, name: string) {
  return main(page).getByTestId("accounts-table").locator("tbody tr").filter({ has: page.locator(`[data-account-name="${name}"]`) });
}

function policyRow(page: Page, project: string) {
  return main(page).getByTestId("policies-table").locator("tbody tr").filter({ has: page.locator(`[data-policy-project="${project}"]`) });
}

/** Two accounts of `me` for the same variable, bound to their first project, and the values the hub holds. */
async function twoAccounts(me: Member): Promise<string[]> {
  const values = [secretValue("account-a"), secretValue("account-b")];
  for (const [index, name] of ["claude-a", "claude-b"].entries()) {
    await putSecretByApi(me, name, { kind: "env", env_var: VAR, projects: [me.projects[0]], value: values[index] });
  }
  return values;
}

/** A run of `me` that a worker leased and started, holding one of their accounts. */
async function heldRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  const credentials = await leaseCredentials(live, run.id);
  const lease = credentials.leases.find((item) => item.env_var === VAR);
  expect(lease?.name).toBe("claude-a");
  return { project, live, run, leaseId: lease?.id ?? 0 };
}

/** The daemon reporting what the agent's CLI said of its usage, as `rate_limit_event`. */
async function rateLimit(live: LiveWorker, runId: number, body: Record<string, unknown>): Promise<{ action: string }> {
  const response = await fetch(`${API_URL}/v1/worker/runs/${runId}/rate-limit`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  expect(response.status).toBe(200);
  return response.json();
}

test("the sidebar leads to Accounts, which points to the Secrets page while there is none", async ({ page, member }) => {
  await member([{ role: "writer", maxLevel: "internal" }]);
  await page.getByTestId("nav-accounts").click();
  await page.waitForURL("**/accounts");
  await expect(page.getByRole("heading", { level: 1, name: "Accounts" })).toBeVisible();
  await expect(page.getByTestId("nav-accounts")).toHaveAttribute("aria-current", "page");
  const empty = main(page).getByTestId("state-empty");
  await expect(empty).toContainText("No account yet");
  await expect(empty).toContainText(VAR);
  await expect(main(page).getByTestId("accounts-secrets-link")).toHaveAttribute("href", "/secrets");
});

test("each account shows its state, usage, runs and holder without a value; Pause, Resume and Edit change it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const values = await twoAccounts(me);
  const { project, live, run, leaseId } = await heldRun(me);
  const windows = { five_hour: { utilization: 0.42, resets_at: RESET } };
  expect((await rateLimit(live, run.id, { lease_id: leaseId, status: "allowed", utilization: 0.42, windows })).action).toBe("none");
  const received = recordResponses(page);

  await page.goto("/accounts");
  const held = row(page, "claude-a");
  await expect(held.getByTestId("account-state")).toHaveAttribute("data-state", "ready");
  await expect(held.getByTestId("account-target")).toContainText(VAR);
  await expect(held.getByTestId("account-usage")).toContainText("42% used");
  await expect(held.getByTestId("account-runs")).toHaveText("1 / 1");
  await expect(held.getByTestId("account-cost")).toContainText("$0.00");
  await expect(held.getByRole("link", { name: `Open run #${run.id} of ${project}` })).toHaveAttribute("href", runPath(project, run.id));
  const other = row(page, "claude-b");
  await expect(other).toContainText("No run");
  await expect(other).toContainText("Not reported");

  // Pause, then resume: each a PATCH with the session's CSRF header, and a toast.
  const [pause] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith("/v1/accounts/claude-b")),
    other.getByRole("button", { name: "Pause account claude-b" }).click(),
  ]);
  expect(pause.headers()["x-evo-csrf"]).toBeTruthy();
  expect(pause.postDataJSON()).toEqual({ paused: true });
  await expect(toast(page, "Account claude-b paused")).toBeVisible();
  await expect(other.getByTestId("account-state")).toHaveAttribute("data-state", "paused");
  await other.getByRole("button", { name: "Resume account claude-b" }).click();
  await expect(toast(page, "Account claude-b resumed")).toBeVisible();
  await expect(other.getByTestId("account-state")).toHaveAttribute("data-state", "ready");

  // Edit refuses a weight out of bounds before anything is sent, then saves both numbers.
  await other.getByRole("button", { name: "Edit the weight and priority of claude-b" }).click();
  const dialog = page.getByTestId("account-dialog");
  await dialog.getByTestId("account-weight").fill("1001");
  await dialog.getByTestId("account-dialog-save").click();
  await expect(dialog.getByTestId("account-weight-error")).toHaveText("Enter a number from 0 to 1000.");
  await dialog.getByTestId("account-weight").fill("3");
  await dialog.getByTestId("account-priority").fill("2");
  const [edit] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith("/v1/accounts/claude-b")),
    dialog.getByTestId("account-dialog-save").click(),
  ]);
  expect(edit.postDataJSON()).toEqual({ weight: 3, priority: 2 });
  await expect(dialog).toBeHidden();
  await expect(other.getByTestId("account-target")).toContainText("weight 3, priority 2");

  await page.reload();
  await expect(row(page, "claude-b").getByTestId("account-target")).toContainText("weight 3, priority 2");
  const bodies = (await received()).join("\n");
  for (const value of values) expect(bodies).not.toContain(value);
  await expect(main(page)).not.toContainText(values[0]);
});

test("the rotation policy of a project is set on the page and reset to the defaults", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await twoAccounts(me);

  await page.goto("/accounts");
  const policy = policyRow(page, project);
  await expect(policy.getByTestId("policy-origin")).toHaveText("Defaults");
  await expect(policy.getByTestId("policy-strategy-text")).toHaveText("Round robin");
  await expect(policy.getByTestId("policy-failover-text")).toHaveText("On, at most 3 times");

  await policy.getByRole("button", { name: `Edit the policy for ${project}` }).click();
  const dialog = page.getByTestId("policy-dialog");
  await dialog.getByTestId("policy-strategy").selectOption("fill-first");
  await dialog.getByTestId("policy-cooldown").fill("30");
  await dialog.getByTestId("policy-failover").click();
  await expect(dialog.getByTestId("policy-max-failovers")).toBeHidden();
  const [put] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith(`/v1/account-policies/${project}`)),
    dialog.getByTestId("policy-dialog-save").click(),
  ]);
  expect(put.headers()["x-evo-csrf"]).toBeTruthy();
  expect(put.postDataJSON()).toEqual({ strategy: "fill-first", cooldown_seconds: 1800, failover: false, max_failovers: 3 });
  await expect(toast(page, `Policy for ${project} saved`)).toBeVisible();
  await expect(policy.getByTestId("policy-origin")).toHaveText("Yours");
  await expect(policy.getByTestId("policy-strategy-text")).toHaveText("Fill first");
  await expect(policy.getByTestId("policy-cooldown-text")).toHaveText("30 minutes");
  await expect(policy.getByTestId("policy-failover-text")).toHaveText("Off: the run stops");

  await policy.getByRole("button", { name: `Reset the policy for ${project} to the defaults` }).click();
  await expect(toast(page, `Policy for ${project} reset`)).toBeVisible();
  await expect(policy.getByTestId("policy-origin")).toHaveText("Defaults");
  await expect(policy.getByTestId("policy-strategy-text")).toHaveText("Round robin");
});

test("the run page names the account a run took and the usage limit it moved on from", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const values = await twoAccounts(me);
  const { project, live, run, leaseId } = await heldRun(me);
  expect((await rateLimit(live, run.id, { lease_id: leaseId, status: "rejected", rate_limit_type: "five_hour", resets_at: RESET })).action).toBe(
    "failover",
  );
  const received = recordResponses(page);

  await page.goto(runPath(project, run.id));
  const card = main(page).getByTestId("run-accounts");
  const taken = card.getByTestId("run-account-item");
  await expect(taken).toHaveAttribute("data-env-var", VAR);
  await expect(taken.getByTestId("run-account-name")).toHaveText("claude-b");
  await expect(taken.getByTestId("run-account-how")).toHaveText("From the pool");
  await expect(taken.getByTestId("run-account-failovers")).toHaveText("1 failover");
  const event = taken.getByTestId("run-account-event");
  await expect(event).toHaveAttribute("data-action", "failover");
  await expect(event).toContainText("claude-a hit its usage limit; the run went on with claude-b");
  await expect(event).toContainText("It cools down until");
  await expect(card.getByTestId("run-accounts-link")).toHaveAttribute("href", "/accounts");

  // The account that hit its limit cools down on the Accounts page, until the reset the CLI gave.
  await card.getByTestId("run-accounts-link").click();
  await page.waitForURL("**/accounts");
  await expect(row(page, "claude-a").getByTestId("account-state")).toHaveAttribute("data-state", "cooling");
  await expect(row(page, "claude-a").getByTestId("account-cooling-until")).toBeVisible();
  await expect(row(page, "claude-b").getByRole("link", { name: `Open run #${run.id} of ${project}` })).toBeVisible();
  const bodies = (await received()).join("\n");
  for (const value of values) expect(bodies).not.toContain(value);
});
