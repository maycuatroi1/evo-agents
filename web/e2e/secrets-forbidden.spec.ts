import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type Account, bearerClient, type HubAdmin, machineToken, newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, leaseCredentials, liveWorker, runPath, seedRunPlan, startRun } from "./support/runs";
import { putSecretByApi, runCredentialsOf, secretsOf, secretValue } from "./support/secrets";
import { csrfHeader, registerWorker } from "./support/workers";

/**
 * A secret belongs to the member who wrote it because the API says so, not because the web hides it: another member,
 * a hub admin included, sees nothing of it on the page and gets 404 deleting it; a reader cannot write one, in the
 * dialog or through the API; a secret binds only to its owner's own workers that are not revoked; a write without the
 * CSRF header is refused; a worker token reaches no secret route; and only the member who dispatched a run reads the
 * leases it got. The calls go straight to the API with the browser's session, CSRF header included.
 */
test.skip(isDeployed, "writes secrets through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** A writer of a new project with a secret of theirs for it, under a name of its own. */
async function ownerWithSecret(admin: HubAdmin) {
  const owner: Account = newAccount("owner");
  const project = uniqueName("vault");
  await admin.registerProject(project);
  await admin.grant(project, owner.login, "writer", "internal");
  const name = uniqueName("oauth");
  const value = secretValue("owner");
  await putSecretByApi(owner, name, { kind: "env", env_var: "OWNER_ONLY_TOKEN", projects: [project], value });
  return { owner, project, name, value };
}

test("another member sees nothing of my secrets, on the page or through the API", async ({ page, admin, member }) => {
  const { owner, project, name, value } = await ownerWithSecret(admin);
  const [mine] = await secretsOf(owner);
  const other = await member([{ role: "writer", maxLevel: "internal" }]);
  await admin.grant(project, other.login, "writer", "internal");

  await page.goto("/secrets");
  await expect(main(page).getByTestId("state-empty")).toBeVisible();
  expect(await page.content()).not.toContain(name);
  expect(await page.content()).not.toContain("OWNER_ONLY_TOKEN");

  const headers = await csrfHeader(page);
  const list = await page.request.get("/v1/secrets");
  expect(list.status()).toBe(200);
  expect(await list.json()).toEqual([]);
  const removed = await page.request.delete(`/v1/secrets/${name}`, { headers });
  expect(removed.status()).toBe(404);
  expect((await removed.json()).error).toBe("not_found");

  // The name is the other member's to use for a secret of their own; mine stays as it was.
  const theirs = await page.request.put(`/v1/secrets/${name}`, {
    headers,
    data: { kind: "env", env_var: "OTHER_TOKEN", projects: [project], value: secretValue("other") },
  });
  expect(theirs.status()).toBe(200);
  expect((await theirs.json()).created).toBe(true);
  expect(await secretsOf(owner)).toEqual([mine]);
  expect(JSON.stringify(await secretsOf(other))).not.toContain(value);
});

test("a reader cannot write a secret, in the dialog or through the API, and no write goes without the CSRF header", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];

  await page.goto("/secrets");
  await main(page).getByTestId("secrets-add").click();
  const dialog = page.getByTestId("secret-dialog");
  await expect(dialog.getByTestId("secret-no-projects")).toContainText("writer role");
  await expect(dialog.getByTestId("secret-save")).toBeDisabled();

  const body = { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: secretValue("reader") };
  const refused = await page.request.put("/v1/secrets/claude-oauth", { data: body, headers: await csrfHeader(page) });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain("needs the writer role");

  const forged = await page.request.put("/v1/secrets/claude-oauth", { data: body });
  expect(forged.status()).toBe(403);
  expect((await forged.json()).message).toContain("X-Evo-CSRF");
  expect(await secretsOf(me)).toEqual([]);
});

test("a secret binds only to my own workers that are not revoked", async ({ page, admin, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const mine = await registerWorker(me, { name: uniqueName("mine"), projects: [project] });
  const gone = await registerWorker(me, { name: uniqueName("gone"), projects: [project] });
  await bearerClient(await machineToken(me)).POST("/v1/workers/{worker_id}/revoke", { params: { path: { worker_id: gone.id } } });
  const colleague = newAccount("colleague");
  await admin.grant(project, colleague.login, "writer", "internal");
  const theirs = await registerWorker(colleague, { name: uniqueName("theirs"), projects: [project] });

  await page.goto("/secrets");
  await main(page).getByTestId("secrets-add").click();
  const workers = page.getByTestId("secret-dialog").getByTestId("secret-workers");
  await expect(workers.getByRole("checkbox")).toHaveCount(1);
  await expect(workers.getByRole("checkbox", { name: mine.name })).toBeVisible();

  const headers = await csrfHeader(page);
  for (const name of [theirs.name, gone.name]) {
    const refused = await page.request.put("/v1/secrets/bound", {
      headers,
      data: { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], workers: [name], value: secretValue("bound") },
    });
    expect(refused.status(), name).toBe(403);
    expect((await refused.json()).message).toContain(`${name} is not a worker of yours`);
  }
  expect(await secretsOf(me)).toEqual([]);
});

test("a hub admin sees only their own secrets and not the credentials of another member's run", async ({ page, admin, signInAs }) => {
  const { owner, project, name, value } = await ownerWithSecret(admin);
  await seedRunPlan(owner, project);
  const live = await liveWorker(owner, project, uniqueName("laptop"));
  const [run] = await dispatch(owner, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  expect((await leaseCredentials(live, run.id)).leases).toHaveLength(1);
  await admin.grant(project, ADMIN_ACCOUNT.login, "reader", "internal");

  // A worker token reaches no secret route, and no run's leases through the owner's route.
  const workerApi = bearerClient(live.token);
  expect((await workerApi.GET("/v1/secrets")).response.status).toBe(403);

  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/secrets");
  await expect(main(page).getByRole("heading", { level: 1, name: "Secrets" })).toBeVisible();
  const listed = await page.request.get("/v1/secrets");
  expect(listed.status()).toBe(200);
  const text = await listed.text();
  expect(text).not.toContain(name);
  expect(text).not.toContain(value);
  expect(await page.content()).not.toContain(name);

  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-details")).toBeVisible();
  await expect(main(page).getByTestId("run-credentials")).toHaveCount(0);
  const leases = await page.request.get(`/v1/projects/${project}/runs/${run.id}/credentials`);
  expect(leases.status()).toBe(403);
  expect((await leases.json()).message).toContain(`only ${owner.login}, who dispatched run ${run.id}`);
  expect((await runCredentialsOf(ADMIN_ACCOUNT, project, run.id)).status).toBe(403);
  expect((await runCredentialsOf(owner, project, run.id)).status).toBe(200);
});
