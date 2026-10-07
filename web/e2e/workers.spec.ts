import type { Page } from "@playwright/test";

import { signOut } from "./support/auth";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, bearerClient, machineToken, newAccount, uniqueName } from "./support/hub";
import { toast } from "./support/toast";
import { CHECKOUTS, heartbeat, joinWithCode, registerWorker, RUNTIMES, workersOf } from "./support/workers";

/**
 * The workers pages against the real API: the list with its summary, facets, search and 10-second refresh; the
 * Register dialog from the form to the machine joining with its pairing code; a worker's page with its runtimes,
 * scope, checkouts and heartbeat strip; drain, resume and revoke behind a typed name; the owner's switch that keeps a
 * worker to runs dispatched from the web; and a worker token on the admin tokens page. Pages render in English, the
 * default.
 */
test.skip(isDeployed, "registers workers through the local stack");

function row(page: Page, name: string) {
  return page.getByTestId("workers-table").locator("tbody tr").filter({ has: page.locator(`[data-worker-name="${name}"]`) });
}

test("the sidebar leads to Workers, which offers to register the first one", async ({ page, member }) => {
  await member([{ role: "writer", maxLevel: "internal" }]);
  await page.getByTestId("nav-workers").click();
  await page.waitForURL("**/workers");
  await expect(page.getByRole("heading", { level: 1, name: "Workers" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText("Workers");
  await expect(page.getByTestId("nav-workers")).toHaveAttribute("aria-current", "page");
  const empty = page.locator("#main").getByTestId("state-empty");
  await expect(empty).toContainText("No worker yet");
  await empty.getByTestId("workers-empty-register").click();
  await expect(page.getByTestId("register-dialog")).toBeVisible();
});

test("the list shows each worker's status, filters by status and search, and refreshes every 10 seconds", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const idle = await registerWorker(me, { name: uniqueName("idle"), projects: [project], slots: 2, labels: ["gpu"] });
  await heartbeat(idle.id, { runtimes: RUNTIMES, checkouts: CHECKOUTS });
  const offline = await registerWorker(me, { name: uniqueName("offline"), projects: [project] });
  const drained = await registerWorker(me, { name: uniqueName("drained"), projects: [project] });
  await heartbeat(drained.id);
  const revoked = await registerWorker(me, { name: uniqueName("revoked"), projects: [project] });
  const api = bearerClient(await machineToken(me));
  await api.POST("/v1/workers/{worker_id}/drain", { params: { path: { worker_id: drained.id } } });
  await api.POST("/v1/workers/{worker_id}/revoke", { params: { path: { worker_id: revoked.id } } });

  await page.goto("/workers");
  const main = page.locator("#main");
  await expect(main.getByTestId("summary-idle").locator("dd").first()).toHaveText("1");
  await expect(main.getByTestId("summary-offline").locator("dd").first()).toHaveText("1");
  await expect(main.getByTestId("summary-free-slots")).toContainText("of 2 slots online");
  await expect(main.getByTestId("workers-table").locator("tbody tr")).toHaveCount(3); // the revoked one is hidden
  await expect(main.getByTestId("workers-list-summary")).toContainText("1 revoked worker is hidden");
  await expect(row(page, idle.name).getByTestId("worker-status")).toHaveText("Idle");
  await expect(row(page, idle.name)).toContainText("Claude Code");
  await expect(row(page, idle.name)).not.toContainText("Codex CLI"); // reported, but missing on the machine
  await expect(row(page, idle.name)).toContainText("0/2");
  await expect(row(page, offline.name).getByTestId("worker-status")).toHaveText("Offline");
  await expect(row(page, drained.name).getByTestId("worker-status")).toHaveText("Draining");

  const facets = main.getByTestId("workers-facets");
  await facets.getByRole("button", { name: /^Offline/ }).click();
  await expect(facets.getByRole("button", { name: /^Offline/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/\?status=offline$/);
  await expect(main.getByTestId("workers-table").locator("tbody tr")).toHaveCount(1);
  await expect(row(page, offline.name)).toBeVisible();

  await facets.getByRole("button", { name: /^Revoked/ }).click();
  await expect(row(page, revoked.name).getByTestId("worker-status")).toHaveText("Revoked");

  await facets.getByRole("button", { name: /^All/ }).click();
  await main.getByTestId("workers-search").fill("gpu"); // a label of the idle worker
  await expect(main.getByTestId("workers-table").locator("tbody tr")).toHaveCount(1);
  await expect(row(page, idle.name)).toBeVisible();
  await expect(page).toHaveURL(/\?q=gpu$/);
  await main.getByTestId("workers-search").fill("no-such-worker");
  await expect(main.getByTestId("state-empty")).toContainText("No worker matches");
  await expect(main.getByTestId("filters-in-use")).toHaveText("Search:no-such-worker");
  await main.getByRole("button", { name: "Clear filters" }).click();
  await expect(main.getByTestId("workers-table").locator("tbody tr")).toHaveCount(3);

  // The machine comes back: the list shows it within one refresh, without a reload.
  await heartbeat(offline.id);
  await expect(row(page, offline.name).getByTestId("worker-status")).toHaveText("Idle", { timeout: 15_000 });
  await expect(main.getByTestId("summary-idle").locator("dd").first()).toHaveText("2");
});

test("registering with a pairing code: the code counts down, and the dialog follows the machine joining", async ({ page, member }) => {
  const me = await member([
    { role: "writer", maxLevel: "internal" },
    { role: "reader", maxLevel: "internal" },
  ]);
  const [writable, readOnly] = me.projects;
  const name = uniqueName("laptop");

  await page.goto("/workers");
  await page.getByTestId("workers-register").click();
  const dialog = page.getByTestId("register-dialog");
  await expect(dialog.getByRole("heading", { name: "Register a worker" })).toBeVisible();
  const choices = dialog.getByTestId("register-projects");
  await expect(choices.getByRole("checkbox", { name: writable })).toBeChecked(); // the only one it can be
  await expect(choices.getByRole("checkbox", { name: readOnly })).toHaveCount(0); // reader only: not offered
  await expect(dialog.getByTestId("register-warning")).toContainText("full permissions");

  await dialog.getByTestId("register-create").click();
  await expect(dialog.getByTestId("register-name")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByTestId("register-name")).toBeFocused();
  await dialog.getByTestId("register-name").fill(name);
  await dialog.getByTestId("register-slots").fill("2");
  await dialog.getByTestId("register-labels").fill("macos, gpu");

  await dialog.getByRole("tab", { name: "CLI only" }).click();
  await expect(dialog.getByTestId("register-cli-command").locator("code")).toHaveText(
    `evo-agents worker register --name ${name} --project ${writable} --slots 2 --label macos --label gpu`,
  );
  await dialog.getByRole("tab", { name: "Pairing code" }).click();
  await dialog.getByTestId("register-create").click();

  const code = (await dialog.getByTestId("pairing-code").textContent())?.trim() ?? "";
  expect(code).toMatch(/^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/);
  await expect(dialog.getByTestId("pairing-expires")).toHaveText(/^Expires in (10:00|9:[0-5]\d)$/);
  const first = await dialog.getByTestId("pairing-expires").textContent();
  await expect(dialog.getByTestId("pairing-expires")).not.toHaveText(first ?? ""); // it ticks
  await expect(dialog.getByTestId("pairing-join-command")).toContainText(`--code ${code}`);
  await expect(dialog.getByTestId("pairing-waiting")).toBeVisible();

  // On the machine: evo-agents worker join --code ...
  const joined = await joinWithCode(code, `${name}.local`);
  expect(joined.status).toBe(201);
  const credential = (await joined.json()) as { token: string; worker: { id: number } };
  expect(credential.token).toMatch(/^evw_/);

  await expect(dialog.getByTestId("pairing-joined")).toContainText(name, { timeout: 10_000 });
  await expect(dialog.getByTestId("pairing-joined-facts")).toContainText(`${name}.local`);
  await expect(toast(page, `${name} joined the hub`)).toBeVisible();
  await dialog.getByTestId("pairing-open-worker").click();
  await page.waitForURL(`**/workers/${credential.worker.id}`);
  await expect(page.locator("#main").getByRole("heading", { level: 1 })).toContainText(name);
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText(name);
  await expect(page.locator("#main").getByTestId("worker-scope")).toContainText("macos");
  await expect(page.locator("#main").getByTestId("worker-offline")).toContainText("No heartbeat yet");

  // The code was single use.
  expect((await joinWithCode(code)).status).toBe(403);
});

test("a worker's page shows what its heartbeat reports, and drain, resume and revoke ask for its name", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("desk"), projects: [me.projects[0]], slots: 2, labels: ["linux"] });
  await heartbeat(worker.id, { runtimes: RUNTIMES, checkouts: CHECKOUTS, secondsAgo: 5 });

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  await expect(main.getByRole("heading", { level: 1 })).toContainText(worker.name);
  await expect(main.getByTestId("worker-status").first()).toHaveText("Idle");
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText(worker.name);
  const runtimes = main.getByTestId("worker-runtimes");
  await expect(runtimes.locator('[data-runtime="claude-code"]')).toContainText("version 2.1.289");
  await expect(runtimes.locator('[data-runtime="claude-code"]')).toContainText("Available");
  await expect(runtimes.locator('[data-runtime="codex"]')).toContainText("Missing");
  await expect(runtimes.locator('[data-runtime="codex"]')).toContainText("not found on PATH");
  await expect(main.getByTestId("worker-checkouts").locator('[data-checkout="example-harness"]')).toContainText("main");
  const scope = main.getByTestId("worker-scope");
  await expect(scope).toContainText(me.projects[0]);
  await expect(scope).toContainText("0 of 2 in use");
  await expect(scope).toContainText(`Full, as ${me.login}`);
  await expect(scope).toContainText(`${me.login} only`);
  await expect(main.getByTestId("worker-last-heartbeat")).toContainText("Last heartbeat");
  const strip = main.getByTestId("heartbeat-strip");
  await expect(strip.locator('[role="img"]')).toHaveAttribute("data-received", /^[1-9]/);
  await expect(strip.locator("[data-state]")).toHaveCount(60);

  // Drain: the name has to be typed.
  await main.getByTestId("worker-drain").click();
  const drain = page.getByTestId("drain-worker-dialog");
  await expect(drain.getByTestId("drain-worker-dialog-name")).toBeFocused();
  await drain.getByTestId("drain-worker-dialog-confirm").click();
  await expect(drain).toContainText(`That is not the worker's name. Type ${worker.name} exactly.`);
  await drain.getByTestId("drain-worker-dialog-name").fill(worker.name);
  await drain.getByTestId("drain-worker-dialog-confirm").click();
  await expect(drain).toBeHidden();
  await expect(toast(page, `${worker.name} is draining`)).toContainText("then claims no new ones");
  await expect(main.getByTestId("worker-status").first()).toHaveText("Draining");
  await expect(main.getByTestId("worker-draining")).toBeVisible();
  expect((await workersOf(me)).find((w) => w.id === worker.id)?.drained_at).not.toBeNull();

  // Resume needs no name: it only lets the worker claim again.
  await main.getByTestId("worker-undrain").click();
  await expect(main.getByTestId("worker-status").first()).toHaveText("Idle");
  await expect(toast(page, `${worker.name} resumed`)).toContainText("It claims runs again.");

  // Revoke: Escape leaves it alone; the typed name ends it.
  await main.getByTestId("worker-revoke").click();
  const revoke = page.getByTestId("revoke-worker-dialog");
  await expect(revoke).toContainText("This cannot be undone");
  await page.keyboard.press("Escape");
  await expect(revoke).toBeHidden();
  await main.getByTestId("worker-revoke").click();
  await revoke.getByTestId("revoke-worker-dialog-name").fill(worker.name);
  await revoke.getByTestId("revoke-worker-dialog-name").press("Enter");
  await expect(main.getByTestId("worker-status").first()).toHaveText("Revoked");
  await expect(main.getByTestId("worker-revoked")).toBeVisible();
  await expect(main.getByTestId("worker-actions")).toHaveCount(0);
  expect((await workersOf(me)).find((w) => w.id === worker.id)?.status).toBe("revoked");
});

test("the owner keeps a worker to runs dispatched from the web, which a token cannot undo", async ({ page, member, signInAs }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("guarded"), projects: [me.projects[0]] });
  await heartbeat(worker.id, { runtimes: RUNTIMES, checkouts: CHECKOUTS });
  const dispatchFromOf = async () => (await workersOf(me)).find((w) => w.id === worker.id)?.dispatch_from;

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  const toggle = main.getByRole("switch", { name: "Only runs dispatched from the web" });
  await expect(toggle).not.toBeChecked();
  await expect(main.getByTestId("worker-dispatch")).toHaveText(`${me.login} only`);

  await toggle.click();
  await expect(toggle).toBeChecked();
  await expect(toast(page, `${worker.name} takes only runs dispatched from the web`)).toContainText("wait for another worker");
  await expect(main.getByTestId("worker-dispatch")).toHaveText(`${me.login} only, from the web`);
  expect(await dispatchFromOf()).toBe("web");

  // The member's own machine token is refused, so a token that leaked cannot open the worker again.
  const api = bearerClient(await machineToken(me));
  const refused = await api.POST("/v1/workers/{worker_id}/dispatch-from", {
    params: { path: { worker_id: worker.id } },
    body: { value: "any" },
  });
  expect(refused.response.status).toBe(403);
  expect(refused.error?.message).toContain("web session only");
  expect(await dispatchFromOf()).toBe("web");

  // A hub admin sees the setting, without the switch.
  await signOut(page); // of the hub and the fake GitHub
  await signInAs(ADMIN_ACCOUNT);
  await page.goto(`/workers/${worker.id}`);
  await expect(main.getByTestId("worker-dispatch")).toHaveText(`${me.login} only, from the web`);
  await expect(main.getByTestId("worker-as-admin")).toBeVisible();
  await expect(main.getByTestId("worker-dispatch-from")).toHaveCount(0);

  // The owner turns it off again, from its label.
  await signOut(page);
  await signInAs(me);
  await page.goto(`/workers/${worker.id}`);
  await expect(toggle).toBeChecked();
  await main.getByTestId("worker-dispatch-from").getByText("Only runs dispatched from the web").click();
  await expect(toggle).not.toBeChecked();
  await expect(toast(page, `${worker.name} takes runs from the command line again`)).toContainText("any of your credentials");
  await expect(main.getByTestId("worker-dispatch")).toHaveText(`${me.login} only`);
  expect(await dispatchFromOf()).toBe("any");
});

test("a worker token on the admin tokens page says so, and revoking it revokes the worker", async ({ page, admin, signInAs }) => {
  const owner = newAccount("worker-owner");
  const project = uniqueName("fleet");
  await admin.registerProject(project);
  await admin.grant(project, owner.login, "writer", "internal");
  const worker = await registerWorker(owner, { name: uniqueName("tokened"), projects: [project] });

  await signInAs(ADMIN_ACCOUNT);
  await page.goto(`/admin/tokens?login=${owner.login}&kind=worker`);
  const table = page.locator("#main").getByTestId("tokens-table");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText("Worker token");
  await table.locator('[data-testid^="revoke-token-"]').click();
  const dialog = page.getByTestId("revoke-token-dialog");
  await expect(dialog).toContainText("revoking its token revokes the worker too");
  await dialog.getByTestId("revoke-token-dialog-confirm").click();
  await expect(toast(page, /Token \d+ of .+ revoked/)).toBeVisible();
  expect((await workersOf(owner)).find((w) => w.id === worker.id)?.status).toBe("revoked");

  await page.goto(`/workers/${worker.id}`); // a hub admin sees every worker, revoked ones included
  await expect(page.locator("#main").getByTestId("worker-revoked")).toBeVisible();
});

test.describe("in Vietnamese", () => {
  test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

  test("the workers page keeps the English terms", async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const worker = await registerWorker(me, { name: uniqueName("vi"), projects: [me.projects[0]] });
    await heartbeat(worker.id);
    await page.goto("/workers");
    await expect(page.locator("#main").getByRole("heading", { level: 1, name: "Worker" })).toBeVisible();
    await expect(row(page, worker.name).getByTestId("worker-status")).toHaveText("Rảnh");
    await page.getByTestId("workers-register").click();
    const dialog = page.getByTestId("register-dialog");
    await expect(dialog.getByRole("heading", { name: "Đăng ký worker" })).toBeVisible();
    await expect(dialog.getByRole("tab", { name: "Pairing code" })).toBeVisible();
  });
});
