import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { agentPath, createAgentByApi, putAgentAccountByApi } from "./support/agents";
import { API_URL } from "./support/env";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { revealNavItem } from "./support/nav";
import { claimRun, dispatch, leaseCredentials, type LiveWorker, liveWorker, openRunDetails, runPath, seedRunPlan, startRun } from "./support/runs";
import { FAKE_MODELS, providerKey, providersOf, putProviderByApi } from "./support/providers";
import { putSecretByApi, recordResponses, secretsOf, secretValue } from "./support/secrets";
import { otherMember, shareByApi } from "./support/sharing";
import { toast } from "./support/toast";
import { PROTOCOL_HEADER, registerWorker } from "./support/workers";

/**
 * The Accounts page and the accounts of a run against the real API (docs/credentials.md, section "Accounts"): a
 * member's env secrets for the same variable, each with its state, its usage limits as a bar per window, its runs and
 * their cost, and the runs holding it; Pause, Resume and Edit on each, and the rotation policy of each project; the run page names the
 * account the run took and each usage limit it hit; a third group lists the accounts of the agents of the visitor's
 * projects. No value is ever sent to the browser. Pages render in English.
 */
test.skip(isDeployed, "writes accounts through the local stack");

const VAR = "CLAUDE_CODE_OAUTH_TOKEN";
const RESET = "2031-05-01T12:00:00Z";

function main(page: Page) {
  return page.locator("#main");
}

/** An account's row with what is under it (usage limits, the variables of a merged line): one group (a tbody) of its table. */
function row(page: Page, name: string) {
  return main(page).locator("tbody").filter({ has: page.locator(`[data-account-name="${name}"]`) });
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
  await revealNavItem(page, "nav-accounts");
  await page.getByTestId("nav-accounts").click();
  await page.waitForURL("**/accounts");
  await expect(page.getByRole("heading", { level: 1, name: "Accounts" })).toBeVisible();
  await expect(page.getByTestId("nav-accounts")).toHaveAttribute("aria-current", "page");
  const empty = main(page).getByTestId("state-empty");
  await expect(empty).toContainText("No model account yet");
  await expect(empty).toContainText(VAR);
  await expect(main(page).getByTestId("accounts-secrets-link")).toHaveAttribute("href", "/secrets");
});

test("Add Claude account writes a token the list then shows without it, and Renew token keeps the account's bindings", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const token = secretValue("setup-token");
  const renewed = secretValue("setup-token-new");
  const received = recordResponses(page);

  await page.goto("/accounts");
  await expect(main(page).getByTestId("accounts-add")).toBeVisible();
  // Add provider opens the catalog, whose Claude entry is the Claude sign-in.
  await main(page).getByTestId("accounts-empty-add").click();
  const catalog = page.getByTestId("provider-dialog");
  await catalog.getByTestId("provider-catalog-entry").and(page.locator('[data-provider-id="claude"]')).click();
  await expect(catalog).toBeHidden();
  let dialog = page.getByTestId("login-dialog");
  await expect(dialog.getByRole("heading", { name: "Add Claude account" })).toBeVisible();
  await expect(dialog.getByTestId("login-steps")).toContainText("claude setup-token");
  await dialog.getByTestId("login-name").fill("claude-web");
  await dialog.getByTestId("login-projects").getByRole("checkbox", { name: project }).check();
  await expect(dialog.getByTestId("login-command-text")).toHaveText(`evo-agents hub account login claude-web --project ${project}`);
  const field = dialog.getByTestId("login-token");
  await expect(field).toHaveAttribute("type", "password");
  expect(await field.getAttribute("name")).toBeNull();
  await field.fill(token);

  const [put] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith("/v1/secrets/claude-web")),
    dialog.getByTestId("login-save").click(),
  ]);
  expect(put.headers()["x-evo-csrf"]).toBeTruthy();
  const body = put.postDataJSON();
  expect(body).toMatchObject({ kind: "env", env_var: VAR, projects: [project], workers: [], value: token });
  // A year from today less a day, at 00:00 UTC.
  expect(Date.parse(body.expires_at) - Date.now()).toBeGreaterThan(362 * 24 * 3600 * 1000);
  await expect(dialog).toBeHidden();
  await expect(toast(page, "Account claude-web added")).toBeVisible();
  const added = row(page, "claude-web");
  // A ready account has no pill: its state is only text for a screen reader.
  await expect(added.getByTestId("account-state")).toHaveAttribute("data-state", "ready");
  await expect(added.getByTestId("account-state")).toHaveClass(/sr-only/);
  await expect(added.getByTestId("account-held-by")).toHaveCount(0);
  await expect(added.getByTestId("account-target")).not.toContainText("weight");
  await expect(added.getByTestId("account-target")).toContainText(VAR);
  await expect(added).toContainText(project);
  expect((await secretsOf(me)).map(({ name, kind, env_var, projects }) => ({ name, kind, env_var, projects }))).toEqual([
    { name: "claude-web", kind: "env", env_var: VAR, projects: [project] },
  ]);

  // Renew token: the name is locked, the bindings are the account's, and only the token and its end change.
  await added.getByRole("button", { name: "Renew the token of claude-web" }).click();
  dialog = page.getByTestId("login-dialog");
  await expect(dialog.getByRole("heading", { name: "Renew the token of claude-web" })).toBeVisible();
  await expect(dialog.getByTestId("login-name")).toHaveValue("claude-web");
  await expect(dialog.getByTestId("login-name")).toHaveAttribute("readonly");
  await expect(dialog.getByTestId("login-kept-projects")).toHaveText(project);
  await expect(dialog.getByTestId("login-command-text")).toHaveText("evo-agents hub account login claude-web");
  await dialog.getByTestId("login-token").fill(renewed);
  const [again] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith("/v1/secrets/claude-web")),
    dialog.getByTestId("login-save").click(),
  ]);
  expect(again.postDataJSON()).toMatchObject({ kind: "env", env_var: VAR, projects: [project], workers: [], value: renewed });
  await expect(dialog).toBeHidden();
  await expect(toast(page, "Token of claude-web renewed")).toBeVisible();

  await page.reload();
  await expect(row(page, "claude-web")).toBeVisible();
  const html = await page.content();
  expect(html).not.toContain(token);
  expect(html).not.toContain(renewed);
  const bodies = (await received()).join("\n");
  expect(bodies).not.toContain(token);
  expect(bodies).not.toContain(renewed);
});

test("a token that ends within 30 days is reminded with its day, on its row and above the list", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const soon = new Date(Date.now() + 10 * 24 * 3600 * 1000);
  soon.setUTCHours(0, 0, 0, 0);
  await putSecretByApi(me, "claude-soon", {
    kind: "env",
    env_var: VAR,
    projects: [me.projects[0]],
    expires_at: soon.toISOString(),
    value: secretValue("soon"),
  });
  await putSecretByApi(me, "claude-later", { kind: "env", env_var: VAR, projects: [me.projects[0]], value: secretValue("later") });

  await page.goto("/accounts");
  const day = soon.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  await expect(row(page, "claude-soon").getByTestId("account-renew-reminder")).toHaveText(`Renew before ${day}`);
  await expect(row(page, "claude-later").getByTestId("account-renew-reminder")).toHaveCount(0);
  const notice = main(page).getByTestId("accounts-renew-notice");
  await expect(notice).toContainText("A token ends within 30 days");
  const entry = notice.getByRole("listitem");
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText("claude-soon");
  await expect(entry).toContainText(`ends ${day}`);
  await notice.getByRole("button", { name: "Renew the token of claude-soon" }).click();
  await expect(page.getByTestId("login-dialog").getByRole("heading", { name: "Renew the token of claude-soon" })).toBeVisible();
});

test("each account shows its state, usage limits, runs and holder without a value; Pause, Resume and Edit change it", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const values = await twoAccounts(me);
  const { project, live, run, leaseId } = await heldRun(me);
  const windows = { five_hour: { utilization: 0.42, resets_at: RESET } };
  expect((await rateLimit(live, run.id, { lease_id: leaseId, status: "allowed", utilization: 0.42, windows })).action).toBe("none");
  // A later report that lists the week alone keeps the session's window.
  const week = { seven_day: { utilization: 0.9, resets_at: RESET } };
  const warned = { lease_id: leaseId, status: "allowed_warning", rate_limit_type: "seven_day", windows: week };
  expect((await rateLimit(live, run.id, warned)).action).toBe("none");
  const received = recordResponses(page);

  await page.goto("/accounts");
  const held = row(page, "claude-a");
  await expect(held.getByTestId("account-state")).toHaveAttribute("data-state", "ready");
  await expect(held.getByTestId("account-target")).toContainText(VAR);
  const usage = held.getByRole("group", { name: "Usage limits of claude-a" });
  await expect(usage).toContainText("Usage limits, reported");
  const session = usage.getByRole("progressbar", { name: "Session (5 hours)" });
  await expect(session).toHaveAttribute("aria-valuenow", "42");
  await expect(session).toHaveAttribute("data-tone", "normal");
  const weekly = usage.getByRole("progressbar", { name: "Weekly, all models" });
  await expect(weekly).toHaveAttribute("aria-valuenow", "90");
  await expect(weekly).toHaveAttribute("data-tone", "warning");
  await expect(usage.getByTestId("account-usage-window")).toHaveText([/Session \(5 hours\)42% usedResets May 1/, /Weekly, all models90% used/]);
  await expect(held.getByTestId("account-runs")).toHaveText("1 / 1");
  await expect(held.getByTestId("account-cost")).toContainText("$0.00");
  await expect(held.getByRole("link", { name: `Open run #${run.id} of ${project}` })).toHaveAttribute("href", runPath(project, run.id));
  const other = row(page, "claude-b");
  await expect(other.getByTestId("account-held-by")).toHaveCount(0);
  await expect(other).not.toContainText("No run");
  // Two accounts for the variable: weight and priority show, and the provider is named.
  await expect(other.getByTestId("account-target")).toContainText("Claude");
  await expect(other.getByTestId("account-target")).toContainText("weight 1, priority 0");
  await expect(other.getByTestId("account-usage")).toHaveText("No usage reported yet");
  await expect(other.getByRole("progressbar")).toHaveCount(0);
  await expectNoSeriousViolations(page, "/accounts with usage limits");

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

  await page.goto("/accounts?tab=rotation");
  await expect(main(page).getByTestId("accounts-tab-rotation")).toHaveAttribute("aria-selected", "true");
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
  // The owner's Accounts are among the details behind Show all details.
  await openRunDetails(page, { all: true });
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

/** Pixels the page is wider than the window: zero when it does not scroll sideways. */
async function overflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

test("model accounts are a card per provider and service credentials their own tab, with the cost of the model accounts alone", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await putSecretByApi(me, "claude-sub", { kind: "env", env_var: VAR, projects: [project], value: secretValue("sub") });
  await putSecretByApi(me, "anthropic-key", { kind: "env", env_var: "ANTHROPIC_API_KEY", projects: [project], value: secretValue("key") });
  await putSecretByApi(me, "lark-id", { kind: "env", env_var: "LARK_APP_ID", projects: [project], value: secretValue("id") });
  await putSecretByApi(me, "lark-secret", { kind: "env", env_var: "LARK_APP_SECRET", projects: [project], value: secretValue("secret") });
  await putSecretByApi(me, "gitlab-token", { kind: "env", env_var: "GITLAB_TOKEN", projects: [project], value: secretValue("gl") });

  await page.goto("/accounts");
  await expect(main(page).getByTestId("accounts-tab-providers")).toHaveAttribute("aria-selected", "true");
  // Claude first, as a card like any other, then Anthropic, its key set as an env secret.
  const cards = main(page).getByTestId("provider-card");
  await expect(cards).toHaveCount(2);
  await expect(cards.locator("h3")).toHaveText(["Claude subscription", "Anthropic"]);
  const claude = cards.and(page.locator('[data-provider-id="claude"]'));
  const anthropic = cards.and(page.locator('[data-provider-id="anthropic"]'));
  await expect(claude.getByTestId("provider-card-count")).toHaveText("1 key");
  await expect(claude.getByTestId("provider-card-host")).toHaveText("Signs in on the worker");
  await expect(anthropic.getByTestId("provider-card-host")).toHaveText("Key set as an env secret");

  // Model accounts: the provider's name as text with its logo, and a cost, or an API value for the subscription.
  await expect(row(page, "claude-sub").getByTestId("account-target")).toContainText("Claude");
  await expect(row(page, "claude-sub").locator('[data-provider="claude"]')).toBeVisible();
  await expect(row(page, "claude-sub").getByTestId("account-cost-label")).toHaveText("API value");
  await row(page, "claude-sub").getByTestId("account-cost-label").hover();
  await expect(page.getByRole("tooltip")).toContainText("not billed per run");
  await expect(row(page, "anthropic-key").getByTestId("account-cost-label")).toHaveText("Cost");
  await expect(claude.getByRole("columnheader", { name: "Runs 24 h / 7 d" })).toBeVisible();
  await expect(main(page).getByTestId("account-renew")).toHaveCount(1);
  // "weight N, priority M" is for a variable with two accounts: here there is none.
  await expect(main(page).getByTestId("account-target").filter({ hasText: "weight" })).toHaveCount(0);
  await expect(main(page).getByTestId("accounts-group-service")).toHaveCount(0);

  // Service credentials: their own tab, kept in the URL; no cost, no usage bars; the two Lark variables are one line.
  await main(page).getByTestId("accounts-tab-services").click();
  await expect(page).toHaveURL(/[?&]tab=services/);
  const services = main(page).getByTestId("accounts-group-service");
  await expect(services.getByRole("heading", { level: 2, name: "Service credentials" })).toBeVisible();
  await expect(services.getByTestId("accounts-group-service-count")).toHaveText("3");
  await expect(main(page).getByTestId("provider-card")).toHaveCount(0);
  await expect(services.getByTestId("account-cost")).toHaveCount(0);
  await expect(services.getByTestId("account-usage-window")).toHaveCount(0);
  await expect(services.getByRole("columnheader", { name: "Last used" })).toBeVisible();
  await expect(row(page, "gitlab-token").getByTestId("account-target")).toContainText("GitLab");
  const toggle = services.getByTestId("service-group-toggle");
  await expect(toggle).toHaveText("Lark, 2 variables");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(services.getByTestId("service-variables")).toHaveCount(0);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const variables = services.getByTestId("service-variables");
  await expect(variables.getByTestId("account-edit")).toHaveCount(2);

  // Pause one variable alone: its own pill, the other stays without one.
  await variables.getByRole("button", { name: "Pause account lark-secret" }).click();
  await expect(toast(page, "Account lark-secret paused")).toBeVisible();
  await expect(variables.locator("li").filter({ hasText: "lark-secret" }).getByTestId("account-state")).toHaveAttribute("data-state", "paused");
  await expect(variables.locator("li").filter({ hasText: "lark-id" }).getByTestId("account-state")).toHaveAttribute("data-state", "ready");
  expect(await overflow(page)).toBe(0);

  // A reload keeps the tab; the arrow keys move between tabs and back to Providers drops it from the URL.
  await page.reload();
  await expect(main(page).getByTestId("accounts-tab-services")).toHaveAttribute("aria-selected", "true");
  await main(page).getByTestId("accounts-tab-services").focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await expect(main(page).getByTestId("accounts-tab-providers")).toHaveAttribute("aria-selected", "true");
  await expect(page).not.toHaveURL(/tab=/);
});

test("a hub with model accounts only says there is no service credential, and the page does not scroll sideways", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  await putSecretByApi(me, "claude-a", { kind: "env", env_var: VAR, projects: [me.projects[0]], value: secretValue("a") });
  await putSecretByApi(me, "claude-b", { kind: "env", env_var: VAR, projects: [me.projects[0]], value: secretValue("b") });
  for (const width of [375, 800]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/accounts");
    await expect(main(page).getByTestId("provider-card-count")).toHaveText("2 keys");
    // Two accounts for one variable: only then do weight and priority show.
    await expect(row(page, "claude-a").getByTestId("account-target")).toContainText("weight 1, priority 0");
    expect(await overflow(page)).toBe(0);
    await page.goto("/accounts?tab=services");
    await expect(main(page).getByTestId("accounts-group-service-empty")).toBeVisible();
    expect(await overflow(page)).toBe(0);
  }
});

test("a share whose account is past its usage ceiling says shared runs wait, and only that one", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  await twoAccounts(me);
  const { project, live, run, leaseId } = await heldRun(me);
  const windows = { five_hour: { utilization: 0.9, resets_at: RESET } };
  expect((await rateLimit(live, run.id, { lease_id: leaseId, status: "allowed_warning", rate_limit_type: "five_hour", windows })).action).toBe("none");
  const lan = await otherMember("lan-tran");
  for (const name of ["claude-a", "claude-b"]) {
    await shareByApi(me, name, { grantee_kind: "user", login: lan.login, projects: [project], usage_ceiling_pct: 80, terms_ack: true });
  }

  await page.goto("/accounts?tab=sharing");
  const shares = main(page).getByTestId("account-shares").getByTestId("share-row");
  await expect(shares).toHaveCount(2);
  const over = shares.filter({ has: page.getByTestId("share-over-ceiling") });
  await expect(over).toHaveCount(1);
  await expect(over.getByTestId("share-secret-name")).toHaveText("claude-a");
  await expect(over.getByTestId("share-over-ceiling")).toHaveText("Over its ceiling: shared runs wait");
});

test("the accounts of the agents of the visitor's projects follow the provider cards, each row with its agent, project and page", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("keys");
  await createAgentByApi(me, project, { handle, name: "Key holder", permissions: {} });
  const gpu = await registerWorker(me, { name: uniqueName("gpu"), projects: [project] });
  const value = secretValue("agent");
  await putAgentAccountByApi(me, project, handle, "openai-agent", { env_var: "OPENAI_API_KEY", value, workers: [gpu.name] });

  // No account of their own: the empty state, then the agents' accounts.
  await page.goto("/accounts");
  await expect(main(page).getByTestId("state-empty")).toBeVisible();
  const agents = main(page).getByTestId("accounts-group-agent");
  await expect(agents.getByRole("heading", { level: 2, name: "Agent accounts" })).toBeVisible();
  await expect(agents.getByTestId("accounts-group-agent-count")).toHaveText("1");

  // With accounts of their own, it comes after the provider cards; service credentials have their own tab.
  await putSecretByApi(me, "claude-a", { kind: "env", env_var: VAR, projects: [project], value: secretValue("a") });
  await putSecretByApi(me, "gitlab-token", { kind: "env", env_var: "GITLAB_TOKEN", projects: [project], value: secretValue("gl") });
  await page.reload();
  await expect(main(page).getByTestId("provider-card")).toHaveCount(1);
  await expect(agents.getByTestId("accounts-group-agent-count")).toHaveText("1");
  await expect(main(page).getByTestId("accounts-group-service")).toHaveCount(0);
  const titles = main(page).locator("section[aria-labelledby^='provider-card-'] h3, section[aria-labelledby^='accounts-group-'] h2");
  await expect(titles).toHaveText(["Claude subscription", "Agent accounts"]);
  const agentRow = agents.locator("tbody").filter({ has: page.locator('[data-account-name="openai-agent"]') });
  await expect(agentRow.getByTestId("agent-account-project")).toHaveText(project);
  await expect(agentRow.getByTestId("agent-avatar")).toHaveAttribute("data-avatar", "initial");
  await expect(agentRow.getByTestId("account-target")).toContainText("OPENAI_API_KEY");
  await expect(agentRow.getByTestId("account-cost-label")).toHaveText("Cost");
  expect(await page.content()).not.toContain(value);
  for (const width of [375, 800]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(agents.getByTestId("accounts-group-agent-count")).toHaveText("1");
    expect(await overflow(page)).toBe(0);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  const link = agentRow.getByTestId("agent-account-agent");
  await expect(link).toHaveText(`@${handle}`);
  await expect(link).toHaveAttribute("href", agentPath(project, handle));
  await link.click();
  await page.waitForURL(`**${agentPath(project, handle)}`);
  await expect(main(page).getByTestId("agent-accounts").locator('[data-account-name="openai-agent"]')).toBeVisible();
});

/** The provider dialog, and an entry of its catalog by the catalog's id. */
function providerDialog(page: Page) {
  return page.getByTestId("provider-dialog");
}

function catalogEntry(page: Page, id: string) {
  return providerDialog(page).getByTestId("provider-catalog-entry").and(page.locator(`[data-provider-id="${id}"]`));
}

function card(page: Page, id: string) {
  return main(page).getByTestId("provider-card").and(page.locator(`[data-provider-id="${id}"]`));
}

const providerPut = (name: string) => (request: { method(): string; url(): string }) =>
  request.method() === "PUT" && request.url().endsWith(`/v1/provider-accounts/${name}`);

test("Add provider searches and filters the catalog, tests the key, fetches the models and adds a card the key never comes back to", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const bad = providerKey(false);
  const key = providerKey();
  const received = recordResponses(page);

  await page.goto("/accounts");
  await main(page).getByTestId("accounts-add").click();
  const dialog = providerDialog(page);
  await expect(dialog).toHaveAttribute("data-step", "catalog");
  await expect(dialog.getByRole("heading", { name: "Add provider" })).toBeVisible();
  // A page of entries at first, more on request; a filter by group, then a search by name or id.
  await expect(dialog.getByTestId("provider-catalog-entry")).toHaveCount(24);
  await expect(dialog.getByTestId("provider-catalog-more")).toBeVisible();
  const regional = dialog.getByTestId("provider-catalog-group").and(page.locator('[data-filter="regional"]'));
  await regional.click();
  await expect(regional).toHaveAttribute("aria-pressed", "true");
  await dialog.getByTestId("provider-catalog-search").fill("glm cod");
  await expect(dialog.getByTestId("provider-catalog-count")).toHaveText("2 providers");
  await dialog.getByTestId("provider-catalog-search").fill("glm think");
  await expect(dialog.getByTestId("provider-catalog-count")).toHaveText("1 provider");
  await expect(dialog.getByTestId("provider-catalog-entry")).toHaveAttribute("data-provider-id", "glmt");
  await dialog.getByTestId("provider-catalog-search").fill("no such provider");
  await expect(dialog.getByTestId("provider-catalog-empty")).toBeVisible();
  await dialog.getByTestId("provider-catalog-search").fill("glm");
  await expectNoSeriousViolations(page, "/accounts provider catalog");
  await catalogEntry(page, "glm").click();

  // The form, prefilled from the catalog.
  await expect(dialog).toHaveAttribute("data-step", "form");
  await expect(dialog.getByRole("heading", { name: "Add GLM Coding" })).toBeVisible();
  await expect(dialog.getByTestId("provider-name")).toHaveValue("glm");
  await expect(dialog.getByTestId("provider-base-url")).toHaveValue("https://api.z.ai/api/coding/paas/v4");
  await expect(dialog.getByTestId("provider-format")).toHaveValue("openai");
  const field = dialog.getByTestId("provider-key");
  await expect(field).toHaveAttribute("type", "password");
  expect(await field.getAttribute("name")).toBeNull();

  // Nothing is sent while a field is wrong; each problem is said by its field.
  await dialog.getByTestId("provider-save").click();
  await expect(dialog.getByTestId("provider-key-error")).toHaveText("Paste the key.");
  await expect(dialog.getByTestId("provider-projects-error")).toHaveText("Pick at least one project.");
  await expect(field).toBeFocused();

  // Test connection: a key the endpoint refuses, then one it takes.
  await field.fill(bad);
  await dialog.getByTestId("provider-test").click();
  await expect(dialog.getByTestId("provider-test-result")).toHaveAttribute("data-ok", "false");
  await field.fill(key);
  await dialog.getByTestId("provider-test").click();
  await expect(dialog.getByTestId("provider-test-result")).toHaveAttribute("data-ok", "true");
  await expect(dialog.getByTestId("provider-test-result")).toHaveText("The endpoint answered and lists 3 models.");

  // Fetch models lists the endpoint's beside the catalog's; the search narrows the list.
  await dialog.getByTestId("provider-fetch-models").click();
  const list = dialog.getByTestId("provider-models-list");
  for (const model of FAKE_MODELS) await expect(list.getByRole("checkbox", { name: model })).toBeVisible();
  await expect(list.getByRole("checkbox", { name: "glm-5.3-flash" })).toBeChecked();
  await dialog.getByTestId("provider-models-search").fill("large");
  await expect(list.getByRole("checkbox")).toHaveCount(1);
  await list.getByRole("checkbox", { name: "fake-large" }).check();
  await dialog.getByTestId("provider-models-search").fill("");
  await expect(dialog.getByTestId("provider-models-picked")).toHaveText("2 of 4 picked");
  await dialog.getByTestId("provider-projects").getByRole("checkbox", { name: project }).check();

  const [put] = await Promise.all([page.waitForRequest(providerPut("glm")), dialog.getByTestId("provider-save").click()]);
  expect(put.headers()["x-evo-csrf"]).toBeTruthy();
  expect(put.postDataJSON()).toEqual({
    provider: "glm",
    base_url: "https://api.z.ai/api/coding/paas/v4",
    api_format: "openai",
    value: key,
    models: ["glm-5.3-flash", "fake-large"],
    projects: [project],
    workers: [],
  });
  await expect(dialog).toBeHidden();
  await expect(toast(page, "Provider account glm added")).toBeVisible();

  // Its card: the provider's logo, the name, the host, the format, its key and its models.
  const glm = card(page, "glm");
  await expect(glm.getByRole("heading", { name: "GLM Coding" })).toBeVisible();
  await expect(glm.locator('[data-provider-mark="glm"] img')).toHaveAttribute("src", "/provider-logos/glm.svg");
  await expect(glm.locator('[data-provider-mark="glm"] img')).toHaveJSProperty("complete", true);
  await expect(glm.getByTestId("provider-card-host")).toHaveText("api.z.ai");
  await expect(glm.getByTestId("provider-card-format")).toContainText("openai");
  await expect(glm.getByTestId("provider-card-count")).toHaveText("1 key");
  await expect(glm.getByTestId("provider-card-model")).toHaveText(["glm-5.3-flash", "fake-large"]);
  await expect(row(page, "glm").getByTestId("account-state")).toHaveAttribute("data-state", "ready");
  // The hub of the specs runs no model gateway: the page says runs cannot take the account yet.
  await expect(main(page).getByTestId("accounts-gateway-notice")).toHaveAttribute("data-state", "off");

  // The Models tab names each model as a run asks for it, by provider, with its runtimes.
  await main(page).getByTestId("accounts-tab-models").click();
  const group = main(page).getByTestId("runnable-models-group").and(page.locator('[data-prefix="glm"]'));
  await expect(group.getByTestId("runnable-model")).toHaveCount(2);
  await expect(group.locator('[data-model="glm/fake-large"]')).toContainText(`Projects: ${project}`);
  await expect(group.getByTestId("runnable-model-runtime").first()).toBeVisible();

  await page.reload();
  const html = await page.content();
  expect(html).not.toContain(key);
  const bodies = (await received()).join("\n");
  expect(bodies).not.toContain(key);
  expect(bodies).not.toContain(bad);
  expect((await providersOf(me)).map(({ name, provider, models }) => ({ name, provider, models }))).toEqual([
    { name: "glm", provider: "glm", models: ["glm-5.3-flash", "fake-large"] },
  ]);
});

test("a self-declared endpoint takes its own base URL, format and model, and a key kept as a secret is used without being sent", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const key = providerKey();
  await putSecretByApi(me, "router-key", { kind: "env", env_var: "E2E_ROUTER_KEY", projects: [project], value: key });
  const received = recordResponses(page);

  // Custom: no base URL from the catalog, which the form asks for, over HTTPS.
  await page.goto("/accounts");
  await main(page).getByTestId("accounts-add").click();
  const dialog = providerDialog(page);
  await dialog.getByTestId("provider-catalog-group").and(page.locator('[data-filter="custom"]')).click();
  await catalogEntry(page, "custom-openai").click();
  await expect(dialog.getByTestId("provider-base-url")).toHaveValue("");
  await dialog.getByTestId("provider-name").fill("my-llm");
  await dialog.getByTestId("provider-key").fill(providerKey());
  await dialog.getByTestId("provider-projects").getByRole("checkbox", { name: project }).check();
  await dialog.getByTestId("provider-save").click();
  await expect(dialog.getByTestId("provider-base-url-error")).toHaveText("Enter the base URL.");
  await dialog.getByTestId("provider-base-url").fill("http://llm.example.test/v1");
  await dialog.getByTestId("provider-save").click();
  await expect(dialog.getByTestId("provider-base-url-error")).toHaveText("The base URL must use HTTPS.");
  await dialog.getByTestId("provider-base-url").fill("https://llm.example.test/v1");
  await dialog.getByTestId("provider-format").selectOption("anthropic");
  await dialog.getByTestId("provider-model-add").fill("bad model");
  await dialog.getByTestId("provider-model-add-button").click();
  await expect(dialog.getByTestId("provider-model-add-error")).toBeVisible();
  await dialog.getByTestId("provider-model-add").fill("my-model-1");
  await dialog.getByTestId("provider-model-add").press("Enter");
  await expect(dialog.getByTestId("provider-models-list").getByRole("checkbox", { name: "my-model-1" })).toBeChecked();
  // The key comes from the secret instead: the field is gone, and nothing but the secret's name is sent.
  await dialog.getByTestId("provider-source-secret").check();
  await expect(dialog.getByTestId("provider-key")).toHaveCount(0);
  await dialog.getByTestId("provider-save").click();
  await expect(dialog.getByTestId("provider-from-secret-error")).toHaveText("Pick the secret that holds the key.");
  await dialog.getByTestId("provider-from-secret").selectOption("router-key");
  await dialog.getByTestId("provider-test").click();
  await expect(dialog.getByTestId("provider-test-result")).toHaveAttribute("data-ok", "true");
  const [put] = await Promise.all([page.waitForRequest(providerPut("my-llm")), dialog.getByTestId("provider-save").click()]);
  expect(put.postDataJSON()).toEqual({
    provider: "custom-openai",
    base_url: "https://llm.example.test/v1",
    api_format: "anthropic",
    from_secret: "router-key",
    models: ["my-model-1"],
    projects: [project],
    workers: [],
  });
  await expect(dialog).toBeHidden();
  const custom = card(page, "custom-openai");
  await expect(custom.getByTestId("provider-card-host")).toHaveText("llm.example.test");
  await expect(custom.getByTestId("provider-card-format")).toContainText("anthropic");
  await expect(custom.getByTestId("provider-card-model")).toHaveText(["my-model-1"]);

  // A name already taken is refused by the form.
  await main(page).getByTestId("accounts-add").click();
  await dialog.getByTestId("provider-catalog-search").fill("custom-openai");
  await catalogEntry(page, "custom-openai").click();
  await dialog.getByTestId("provider-name").fill("router-key");
  await dialog.getByTestId("provider-save").click();
  await expect(dialog.getByTestId("provider-name-error")).toHaveText("You already have a secret or an account named router-key.");
  await dialog.getByTestId("provider-back").click();
  await expect(dialog).toHaveAttribute("data-step", "catalog");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  const bodies = (await received()).join("\n");
  expect(bodies).not.toContain(key);
});

test("a provider account is edited without its key, its key changed, paused and deleted, on a page that never scrolls sideways", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const first = providerKey();
  const second = providerKey();
  await putProviderByApi(me, "glm-api", { provider: "glm", value: first, projects: [project], models: ["fake-large"] });
  const received = recordResponses(page);

  await page.goto("/accounts");
  const glm = card(page, "glm");
  await expect(glm.getByTestId("provider-card-model")).toHaveText(["fake-large"]);
  const account = row(page, "glm-api");
  await expectNoSeriousViolations(page, "/accounts with a provider account");

  // Edit: the name is locked and no key is asked for; only what changed is sent.
  await account.getByRole("button", { name: "Edit provider account glm-api" }).click();
  const dialog = providerDialog(page);
  await expect(dialog.getByTestId("provider-form")).toHaveAttribute("data-mode", "edit");
  await expect(dialog.getByTestId("provider-name")).toHaveAttribute("readonly");
  await expect(dialog.getByTestId("provider-key")).toHaveCount(0);
  await expect(dialog.getByTestId("provider-back")).toHaveCount(0);
  await dialog.getByTestId("provider-fetch-models").click();
  await dialog.getByTestId("provider-models-list").getByRole("checkbox", { name: "fake-small" }).check();
  await dialog.getByTestId("provider-weight").fill("3");
  const [patch] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith("/v1/provider-accounts/glm-api")),
    dialog.getByTestId("provider-save").click(),
  ]);
  expect(patch.postDataJSON()).toEqual({ models: ["fake-large", "fake-small"], weight: 3 });
  await expect(dialog).toBeHidden();
  await expect(toast(page, "Provider account glm-api changed")).toBeVisible();
  await expect(glm.getByTestId("provider-card-model")).toHaveText(["fake-large", "fake-small"]);

  // Change key: the new key alone.
  await account.getByRole("button", { name: "Change the key of glm-api" }).click();
  const keyDialog = page.getByTestId("provider-key-dialog");
  await keyDialog.getByTestId("provider-key").fill(second);
  const [change] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith("/v1/provider-accounts/glm-api")),
    keyDialog.getByTestId("provider-key-dialog-save").click(),
  ]);
  expect(change.postDataJSON()).toEqual({ value: second });
  await expect(keyDialog).toBeHidden();
  await expect(toast(page, "Key of glm-api changed")).toBeVisible();

  // Pause and resume, as any account.
  await account.getByRole("button", { name: "Pause account glm-api" }).click();
  await expect(toast(page, "Account glm-api paused")).toBeVisible();
  await expect(account.getByTestId("account-state")).toHaveAttribute("data-state", "paused");
  await account.getByRole("button", { name: "Resume account glm-api" }).click();
  await expect(account.getByTestId("account-state")).toHaveAttribute("data-state", "ready");

  // No sideways scroll at a phone's width or a tablet's, in light and dark, the dialog open or not.
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    for (const width of [375, 800]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(glm).toBeVisible();
      expect(await overflow(page)).toBe(0);
      await main(page).getByTestId("accounts-add").click();
      await expect(providerDialog(page).getByTestId("provider-catalog-entry").first()).toBeVisible();
      expect(await overflow(page)).toBe(0);
      await providerDialog(page).getByTestId("provider-catalog-search").fill("glm");
      await catalogEntry(page, "glm").click();
      await expect(providerDialog(page).getByTestId("provider-form")).toBeVisible();
      expect(await overflow(page)).toBe(0);
      await page.keyboard.press("Escape");
      await expect(providerDialog(page)).toBeHidden();
    }
  }
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 1280, height: 900 });

  // Delete, after a confirm that names it: the card goes, and the empty state says what to do.
  await account.getByRole("button", { name: "Delete provider account glm-api" }).click();
  const confirm = page.getByTestId("provider-delete-dialog");
  await expect(confirm.getByRole("heading", { name: "Delete glm-api?" })).toBeVisible();
  await Promise.all([
    page.waitForRequest((request) => request.method() === "DELETE" && request.url().endsWith("/v1/provider-accounts/glm-api")),
    confirm.getByRole("button", { name: "Delete account" }).click(),
  ]);
  await expect(confirm).toBeHidden();
  await expect(toast(page, "Provider account glm-api deleted")).toBeVisible();
  await expect(glm).toHaveCount(0);
  await expect(main(page).getByTestId("state-empty")).toContainText("No model account yet");
  expect(await providersOf(me)).toEqual([]);

  const bodies = (await received()).join("\n");
  expect(bodies).not.toContain(first);
  expect(bodies).not.toContain(second);
});
