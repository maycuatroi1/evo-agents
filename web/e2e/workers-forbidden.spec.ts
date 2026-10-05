import { call } from "../src/lib/api/client";

import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type Account, bearerClient, type HubAdmin, machineToken, newAccount, uniqueName } from "./support/hub";
import { csrfHeader, heartbeat, registerWorker, workersOf } from "./support/workers";

/**
 * A worker belongs to the member who registered it because the API says so, not because the web hides it: another
 * member sees nothing of it on the pages and gets 404 from every workers route, a reader cannot create a pairing
 * code, a web session cannot register a machine directly, and a hub admin may drain or revoke a worker but never set
 * it going again. The calls go straight to the API with the browser's session, CSRF header included.
 */
test.skip(isDeployed, "registers workers through the local stack");

/** A writer of a new project with a worker of theirs that sent a heartbeat just now. */
async function ownerWithWorker(admin: HubAdmin) {
  const owner: Account = newAccount("owner");
  const project = uniqueName("fleet");
  await admin.registerProject(project);
  await admin.grant(project, owner.login, "writer", "internal");
  const worker = await registerWorker(owner, { name: uniqueName("private"), projects: [project] });
  await heartbeat(worker.id);
  return { owner, project, worker };
}

test("another member sees nothing of my worker, on the pages or through the API", async ({ page, admin, member }) => {
  const { owner, project, worker } = await ownerWithWorker(admin);
  const ownerApi = bearerClient(await machineToken(owner));
  const pairing = await call(ownerApi.POST("/v1/workers/pairings", { body: { name: uniqueName("pair"), projects: [project], slots: 1, allow_web_terminal: false } }));

  // Another writer, of another project; then of the same project too.
  const other = await member([{ role: "writer", maxLevel: "internal" }]);
  await admin.grant(project, other.login, "writer", "internal");

  await page.goto("/workers");
  await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
  await expect(page.locator(`[data-worker-name="${worker.name}"]`)).toHaveCount(0);
  await page.goto(`/workers/${worker.id}`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toBeVisible();
  await expect(page.locator("#main")).not.toContainText(worker.name);

  const headers = await csrfHeader(page);
  const list = await page.request.get("/v1/workers?revoked=true");
  expect(list.status()).toBe(200);
  expect(await list.json()).toEqual([]);
  const calls: { method: "GET" | "POST"; path: string }[] = [
    { method: "GET", path: `/v1/workers/${worker.id}` },
    { method: "POST", path: `/v1/workers/${worker.id}/drain` },
    { method: "POST", path: `/v1/workers/${worker.id}/undrain` },
    { method: "POST", path: `/v1/workers/${worker.id}/revoke` },
    { method: "GET", path: `/v1/workers/pairings/${pairing.id}` },
  ];
  for (const { method, path } of calls) {
    const response = await page.request.fetch(path, { method, headers });
    expect(response.status(), `${method} ${path}`).toBe(404);
    expect((await response.json()).error).toBe("not_found");
  }
  // Nothing changed for the owner.
  const mine = (await workersOf(owner)).find((w) => w.id === worker.id);
  expect(mine).toMatchObject({ status: "online", drained_at: null, revoked_at: null });
});

test("a reader cannot create a pairing code, in the dialog or through the API", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];

  await page.goto("/workers");
  await page.getByTestId("workers-register").click();
  const dialog = page.getByTestId("register-dialog");
  await expect(dialog.getByTestId("register-no-projects")).toContainText("writer role");
  await expect(dialog.getByTestId("register-create")).toBeDisabled();

  const headers = await csrfHeader(page);
  const body = { name: uniqueName("reader"), projects: [project], slots: 1, labels: [], allow_web_terminal: false };
  const refused = await page.request.post("/v1/workers/pairings", { data: body, headers });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain("needs the writer role");

  // Without the CSRF header a cookie write is refused before anything else is looked at.
  const forged = await page.request.post("/v1/workers/pairings", { data: body });
  expect(forged.status()).toBe(403);
  expect((await forged.json()).message).toContain("X-Evo-CSRF");

  // A web session cannot register a machine directly; that needs the machine's own token.
  const direct = await page.request.post("/v1/workers", {
    data: { ...body, hostname: "web.local", os: "macOS", arch: "arm64", agent_version: "0.3.0" },
    headers,
  });
  expect(direct.status()).toBe(403);
  expect(await workersOf(me)).toEqual([]);
});

test("a hub admin sees every worker and may drain it, but only its owner resumes it", async ({ page, admin, signInAs }) => {
  const { owner, worker } = await ownerWithWorker(admin);
  await signInAs(ADMIN_ACCOUNT);

  await page.goto("/workers");
  const row = page.locator("#main").getByTestId("workers-table").locator("tbody tr").filter({
    has: page.locator(`[data-worker-name="${worker.name}"]`),
  });
  await expect(row).toContainText(owner.login);

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  await expect(main.getByTestId("worker-as-admin")).toContainText(`only ${owner.login} can resume it`);
  await main.getByTestId("worker-drain").click();
  const dialog = page.getByTestId("drain-worker-dialog");
  await expect(dialog).toContainText(`Only ${owner.login} can resume it afterwards.`);
  await dialog.getByTestId("drain-worker-dialog-name").fill(worker.name);
  await dialog.getByTestId("drain-worker-dialog-confirm").click();
  await expect(main.getByTestId("worker-status").first()).toHaveText("Draining");
  await expect(main.getByTestId("worker-undrain")).toHaveCount(0); // not offered to an admin

  const refused = await page.request.post(`/v1/workers/${worker.id}/undrain`, { headers: await csrfHeader(page) });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain(`only ${owner.login}`);
  expect((await workersOf(owner)).find((w) => w.id === worker.id)?.status).toBe("draining");

  // The owner can.
  const ownerApi = bearerClient(await machineToken(owner));
  const resumed = await call(ownerApi.POST("/v1/workers/{worker_id}/undrain", { params: { path: { worker_id: worker.id } } }));
  expect(resumed.status).toBe("online");
});
