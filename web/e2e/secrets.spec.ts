import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { claimRun, dispatch, leaseCredentials, liveWorker, seedRunPlan, startRun } from "./support/runs";
import { putSecretByApi, recordResponses, type RunLease, runCredentialsOf, secretsOf, secretValue } from "./support/secrets";
import { registerWorker } from "./support/workers";

/**
 * The Secrets page against the real API (docs/credentials.md): a member adds, replaces and deletes their own secrets.
 * A value goes to the hub once, in a password field, with the session's CSRF header, and comes back nowhere: not in the
 * page, not in anything the browser receives, not after a reload, and a replace asks for it again. The form refuses
 * what the hub would refuse before anything is sent. Pages render in English, the default.
 */
test.skip(isDeployed, "writes secrets through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function row(page: Page, name: string) {
  return main(page).getByTestId("secrets-table").locator("tbody tr").filter({ has: page.locator(`[data-secret-name="${name}"]`) });
}

/** A day `days` from now, as a date input takes it. */
function dayFromNow(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

test("the sidebar leads to Secrets, which offers to add the first one", async ({ page, member }) => {
  await member([{ role: "writer", maxLevel: "internal" }]);
  await page.getByTestId("nav-secrets").click();
  await page.waitForURL("**/secrets");
  await expect(page.getByRole("heading", { level: 1, name: "Secrets" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText("Secrets");
  await expect(page.getByTestId("nav-secrets")).toHaveAttribute("aria-current", "page");
  const empty = main(page).getByTestId("state-empty");
  await expect(empty).toContainText("No secret yet");
  await expect(empty).toContainText("evo-agents hub secret set");
  await empty.getByTestId("secrets-empty-add").click();
  await expect(page.getByTestId("secret-dialog")).toHaveAttribute("data-mode", "add");
});

test("a secret added on the page is listed without its value, which never comes back, and Replace asks for it again", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const first = secretValue("oauth");
  const second = secretValue("oauth-new");
  const received = recordResponses(page);

  await page.goto("/secrets");
  await main(page).getByTestId("secrets-add").click();
  let dialog = page.getByTestId("secret-dialog");
  await expect(dialog.getByRole("heading", { name: "Add a secret" })).toBeVisible();
  await dialog.getByTestId("secret-name").fill("claude-oauth");
  await dialog.getByTestId("secret-env-var").fill("CLAUDE_CODE_OAUTH_TOKEN");
  await dialog.getByTestId("secret-projects").getByRole("checkbox", { name: project }).check();
  await expect(dialog.getByTestId("secret-no-workers")).toContainText("no worker yet");
  const field = dialog.getByTestId("secret-value");
  await expect(field).toHaveAttribute("type", "password");
  expect(await field.getAttribute("name")).toBeNull();
  await field.fill(first);

  const [put] = await Promise.all([
    page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith("/v1/secrets/claude-oauth")),
    dialog.getByTestId("secret-save").click(),
  ]);
  expect(put.headers()["x-evo-csrf"]).toBeTruthy();
  expect(put.postDataJSON()).toEqual({
    kind: "env",
    env_var: "CLAUDE_CODE_OAUTH_TOKEN",
    projects: [project],
    workers: [],
    expires_at: null,
    value: first,
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("admin-notice-status")).toContainText("Secret claude-oauth added.");
  const listed = row(page, "claude-oauth");
  await expect(listed).toContainText("CLAUDE_CODE_OAUTH_TOKEN");
  await expect(listed.getByTestId("secret-kind-badge")).toHaveText("Variable");
  await expect(listed).toContainText(project);
  await expect(listed).toContainText("Any of yours");
  await expect(listed).toContainText("No end");

  // The value is nowhere the browser can reach: the page, the list the hub answers, the page read again.
  expect(await page.content()).not.toContain(first);
  const list = await page.request.get("/v1/secrets");
  expect(list.status()).toBe(200);
  const secrets = (await list.json()) as Record<string, unknown>[];
  expect(secrets).toHaveLength(1);
  expect(secrets[0]).not.toHaveProperty("value");
  expect(JSON.stringify(secrets)).not.toContain(first);
  await page.reload();
  await expect(row(page, "claude-oauth")).toBeVisible();
  expect(await page.content()).not.toContain(first);
  const before = (await secretsOf(me))[0];

  // Replace shows the secret as the hub holds it, but an empty value field: the value is typed again.
  await row(page, "claude-oauth").getByTestId("secret-replace").click();
  dialog = page.getByTestId("secret-dialog");
  await expect(dialog).toHaveAttribute("data-mode", "replace");
  await expect(dialog.getByRole("heading", { name: "Replace secret claude-oauth" })).toBeVisible();
  await expect(dialog.getByTestId("secret-name")).toHaveCount(0);
  await expect(dialog.getByTestId("secret-env-var")).toHaveValue("CLAUDE_CODE_OAUTH_TOKEN");
  await expect(dialog.getByTestId("secret-projects").getByRole("checkbox", { name: project })).toBeChecked();
  await expect(dialog.getByTestId("secret-value")).toHaveValue("");
  await expect(dialog.getByTestId("secret-value")).toHaveAttribute("type", "password");
  await dialog.getByTestId("secret-save").click();
  await expect(dialog.getByTestId("secret-value-error")).toHaveText("Type or paste the value.");
  await dialog.getByTestId("secret-value").fill(second);
  await dialog.getByTestId("secret-save").click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("admin-notice-status")).toContainText("Secret claude-oauth replaced.");
  const after = (await secretsOf(me))[0];
  expect(Date.parse(after.updated_at)).toBeGreaterThan(Date.parse(before.updated_at));
  expect(after).toMatchObject({ name: "claude-oauth", kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project] });

  // Opened again, the form has no value in it, and nothing the browser received ever held either value.
  await row(page, "claude-oauth").getByTestId("secret-replace").click();
  await expect(page.getByTestId("secret-value")).toHaveValue("");
  await page.keyboard.press("Escape");
  const html = await page.content();
  expect(html).not.toContain(first);
  expect(html).not.toContain(second);
  const bodies = await received();
  expect(bodies.length).toBeGreaterThan(5);
  for (const body of bodies) {
    expect(body.includes(first) || body.includes(second), "a response the browser received holds a value").toBe(false);
  }
});

test("a git secret for one worker with an end date, once the form's checks pass", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const mini = await registerWorker(me, { name: uniqueName("mini"), projects: [project] });
  const spare = await registerWorker(me, { name: uniqueName("spare"), projects: [project] });
  const value = secretValue("gitlab");
  const end = dayFromNow(30);
  let puts = 0;
  page.on("request", (request) => {
    if (request.method() === "PUT") puts += 1;
  });

  await page.goto("/secrets");
  await main(page).getByTestId("secrets-add").click();
  const dialog = page.getByTestId("secret-dialog");
  await dialog.getByTestId("secret-kind-git").click();
  await dialog.getByTestId("secret-name").fill("GitLab Docs");
  await dialog.getByTestId("secret-url-prefix").fill("http://gitlab.example.org/group");
  await dialog.getByTestId("secret-expires").fill("2020-01-01");
  await dialog.getByTestId("secret-save").click();
  await expect(dialog.getByTestId("secret-name-error")).toContainText("Lower case letters");
  await expect(dialog.getByTestId("secret-name")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByTestId("secret-name")).toBeFocused();
  await expect(dialog.getByTestId("secret-url-prefix-error")).toContainText("An https address");
  await expect(dialog.getByTestId("secret-projects-error")).toHaveText("Choose at least one project.");
  await expect(dialog.getByTestId("secret-expires-error")).toContainText("Pick a day after today");
  await expect(dialog.getByTestId("secret-value-error")).toHaveText("Type or paste the value.");

  await dialog.getByTestId("secret-name").fill("gitlab-docs");
  await dialog.getByTestId("secret-url-prefix").fill("https://oauth2:pw@gitlab.example.org/group");
  await dialog.getByTestId("secret-save").click();
  await expect(dialog.getByTestId("secret-url-prefix-error")).toContainText("No user or password");
  expect(puts).toBe(0); // nothing went to the hub while the form was wrong

  await dialog.getByTestId("secret-url-prefix").fill("https://GitLab.example.org/group/");
  await dialog.getByTestId("secret-projects").getByRole("checkbox", { name: project }).check();
  const workers = dialog.getByTestId("secret-workers");
  await expect(workers.getByRole("checkbox")).toHaveCount(2);
  await workers.getByRole("checkbox", { name: mini.name }).check();
  await dialog.getByTestId("secret-expires").fill(end);
  await dialog.getByTestId("secret-value").fill(value);
  await dialog.getByTestId("secret-save").click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("admin-notice-status")).toContainText("Secret gitlab-docs added.");

  const listed = row(page, "gitlab-docs");
  await expect(listed.getByTestId("secret-kind-badge")).toHaveText("Git credential");
  await expect(listed).toContainText("https://gitlab.example.org/group as oauth2"); // as the hub keeps it
  await expect(listed).toContainText(mini.name);
  await expect(listed).not.toContainText(spare.name);
  await expect(listed.getByTestId("secret-expired")).toHaveCount(0);
  expect(await page.content()).not.toContain(value);
  expect(await secretsOf(me)).toEqual([
    expect.objectContaining({
      name: "gitlab-docs",
      kind: "git",
      url_prefix: "https://gitlab.example.org/group",
      username: "oauth2",
      projects: [project],
      workers: [mini.name],
    }),
  ]);
  const kept = (await secretsOf(me))[0];
  expect(new Date(kept.expires_at ?? "").toISOString()).toBe(`${end}T00:00:00.000Z`);
});

test("Delete asks first, then the secret is gone and the leases of it still out are revoked", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const value = secretValue("delete");
  await putSecretByApi(me, "claude-oauth", { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value });
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  const leased = await leaseCredentials(live, run.id);
  expect(leased.leases.map((lease) => [lease.name, lease.value])).toEqual([["claude-oauth", value]]);

  await page.goto("/secrets");
  await row(page, "claude-oauth").getByTestId("secret-delete").click();
  const dialog = page.getByTestId("delete-secret-dialog");
  await expect(dialog.getByRole("heading", { name: "Delete secret claude-oauth?" })).toBeVisible();
  await expect(dialog).toContainText("leases of it still out are revoked");
  await dialog.getByTestId("delete-secret-dialog-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("admin-notice-status")).toContainText("Secret claude-oauth deleted.");
  await expect(main(page).getByTestId("state-empty")).toBeVisible();
  expect(await secretsOf(me)).toEqual([]);

  const credentials = await runCredentialsOf(me, project, run.id);
  expect(credentials.status).toBe(200);
  const leases = credentials.body as RunLease[];
  expect(leases).toHaveLength(1);
  expect(leases[0]).toMatchObject({ name: "claude-oauth", provider: "secret", kind: "env", target: "CLAUDE_CODE_OAUTH_TOKEN" });
  expect(leases[0].revoked_at).not.toBeNull();
  expect(JSON.stringify(leases)).not.toContain(value);
});
