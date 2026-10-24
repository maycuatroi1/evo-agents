import type { Page } from "@playwright/test";

import { signOut } from "./support/auth";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, bearerClient, machineToken, newAccount, uniqueName } from "./support/hub";
import { claimRun, dispatch, liveWorker, READY_STEPS, seedRunPlan } from "./support/runs";
import { toast } from "./support/toast";
import { AGENT_COMMIT, CHECKOUTS, heartbeat, joinWithCode, NIGHTLY_COMMIT, registerWorker, RUNTIMES, workersOf } from "./support/workers";

/**
 * The workers pages against the real API: the list with its summary, facets, search and 10-second refresh; the
 * Register dialog from the form to the machine joining with its pairing code; a worker's page with its runtimes,
 * scope, checkouts and heartbeat strip; drain, resume and revoke behind a typed name; the owner's settings (name, slots
 * and labels); the owner's switch that keeps a worker to runs dispatched from the web; the evo-agents a worker runs, its
 * owner's version lock and the notes and pills that say only what went wrong with its updates; and a worker token on
 * the admin tokens page. Pages render in English, the default.
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
  await expect(main.getByTestId("worker-settings")).toHaveCount(0); // the values show in Scope, without the form

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

test("the owner changes a worker's slots, labels, plugins and name, and the hub's refusals show at the form", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("busy"));
  const other = await registerWorker(me, { name: uniqueName("taken"), projects: [project] });
  await dispatch(me, project, READY_STEPS);
  expect(await claimRun(live)).not.toBeNull();
  expect(await claimRun(live)).toBeNull(); // its one slot is taken

  await page.goto(`/workers/${live.worker.id}`);
  const main = page.locator("#main");
  const settings = main.getByTestId("worker-settings");
  const name = settings.getByLabel("Name");
  const slots = settings.getByLabel("Slots");
  const labels = settings.getByLabel("Labels");
  const plugins = settings.getByLabel("Plugins");
  await expect(name).toHaveValue(live.worker.name);
  await expect(plugins).toHaveValue("");
  await expect(main.getByTestId("worker-plugins")).toHaveText("None");
  await expect(slots).toHaveValue("1");
  await expect(main.getByTestId("worker-scope")).toContainText("1 of 1 in use");
  await expect(main.getByTestId("worker-runs")).toContainText("Holds 1 run now, no slot free.");
  await expect(settings.getByTestId("worker-settings-save")).toBeDisabled(); // nothing changed yet

  // Out of range: said at the field, and nothing is sent.
  await slots.fill("17");
  await settings.getByTestId("worker-settings-save").click();
  await expect(settings).toContainText("Enter a whole number from 1 to 16.");
  await expect(slots).toHaveAttribute("aria-invalid", "true");
  await expect(slots).toBeFocused();

  // A name another worker of mine has: the hub's 409 shows at the name field.
  await slots.fill("2");
  await name.fill(other.name);
  await settings.getByTestId("worker-settings-save").click();
  await expect(settings).toContainText(`You have a worker named ${other.name} already.`);
  await expect(name).toHaveAttribute("aria-invalid", "true");
  expect((await workersOf(me)).find((w) => w.id === live.worker.id)).toMatchObject({ name: live.worker.name, slots: 1 });

  // Discard puts back what the hub holds.
  await settings.getByTestId("worker-settings-discard").click();
  await expect(name).toHaveValue(live.worker.name);
  await expect(settings.getByTestId("worker-settings-discard")).toHaveCount(0);

  // A plugin id the hub does not take: said at the field, and nothing is sent.
  await plugins.fill("Sandbox");
  await settings.getByTestId("worker-settings-save").click();
  await expect(settings).toContainText("Not a plugin id: Sandbox.");
  await expect(plugins).toHaveAttribute("aria-invalid", "true");
  await expect(plugins).toBeFocused();

  const renamed = uniqueName("roomy");
  await name.fill(renamed);
  await slots.fill("2");
  await labels.fill("gpu, linux");
  await plugins.fill("sandbox");
  await settings.getByTestId("worker-settings-save").click();
  await expect(toast(page, `Settings of ${renamed} saved`)).toContainText("next heartbeat");
  await expect(main.getByRole("heading", { level: 1 })).toContainText(renamed);
  await expect(main.getByTestId("worker-scope")).toContainText("1 of 2 in use");
  await expect(main.getByTestId("worker-scope")).toContainText("linux");
  await expect(main.getByTestId("worker-plugins")).toHaveText("sandbox");
  await expect(main.getByTestId("worker-runs")).toContainText("Holds 1 run now, 1 slot free.");
  await expect(slots).toHaveValue("2");
  await expect(settings.getByTestId("worker-settings-save")).toBeDisabled();
  expect((await workersOf(me)).find((w) => w.id === live.worker.id)).toMatchObject({ name: renamed, slots: 2, labels: ["gpu", "linux"], plugins: ["sandbox"] });
  expect(await claimRun(live)).not.toBeNull(); // the hub counts the held runs against the new slots

  // The list shows the new values.
  await page.goto("/workers");
  await expect(row(page, renamed)).toContainText("2/2");
  await page.locator("#main").getByTestId("workers-search").fill("linux"); // a new label
  await expect(page.locator("#main").getByTestId("workers-table").locator("tbody tr")).toHaveCount(1);
  await expect(row(page, renamed)).toBeVisible();
});

test("the owner adds and drops the projects a worker serves, and a project it still needs is refused at the field", async ({ page, member }) => {
  const me = await member([
    { role: "writer", maxLevel: "internal" },
    { role: "writer", maxLevel: "internal" },
  ]);
  const [first, second] = me.projects;
  await seedRunPlan(me, first);
  const live = await liveWorker(me, first, uniqueName("serving"));
  await dispatch(me, first, READY_STEPS);
  expect(await claimRun(live)).not.toBeNull(); // it holds a run of the first project
  const projectsOf = async () => (await workersOf(me)).find((w) => w.id === live.worker.id)?.projects;

  await page.goto(`/workers/${live.worker.id}`);
  const main = page.locator("#main");
  const settings = main.getByTestId("worker-settings");
  const projects = settings.getByTestId("worker-settings-projects");
  const firstBox = projects.getByRole("checkbox", { name: first, exact: true });
  const secondBox = projects.getByRole("checkbox", { name: second, exact: true });
  await expect(firstBox).toBeChecked();
  await expect(secondBox).not.toBeChecked();

  // None at all: said at the field, and nothing is sent.
  await firstBox.uncheck();
  await settings.getByTestId("worker-settings-save").click();
  await expect(settings).toContainText("Choose one project at least.");

  // Dropping the project of the run it holds: the hub's 409 shows at the field, and nothing changes.
  await secondBox.check();
  await settings.getByTestId("worker-settings-save").click();
  await expect(settings).toContainText("The hub keeps the projects as they were");
  await expect(settings).toContainText(`cannot stop serving ${first}`);
  expect(await projectsOf()).toEqual([first]);

  await firstBox.check();
  await settings.getByTestId("worker-settings-save").click();
  await expect(toast(page, `Settings of ${live.worker.name} saved`)).toContainText("next heartbeat");
  expect(await projectsOf()).toEqual([first, second].sort());
  await expect(main.getByTestId("worker-scope")).toContainText(second);
  await expect(settings.getByTestId("worker-settings-save")).toBeDisabled();
});

test("a worker's page links the commit it runs, and its owner locks its version, the hub's refusal shown at the switch", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("pinned"), projects: [me.projects[0]] });
  await heartbeat(worker.id, {
    agentCommit: AGENT_COMMIT,
    selfUpdate: { state: "current", nightly_commit: AGENT_COMMIT, checked_at: new Date().toISOString() },
  });
  const lockedOf = async () => (await workersOf(me)).find((w) => w.id === worker.id)?.version_locked;

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  const version = main.getByTestId("worker-version");
  await expect(version.getByTestId("worker-agent-version")).toHaveText("0.4.0");
  const commit = version.getByRole("link", { name: "ca3e9af on GitHub (opens in a new tab)" });
  await expect(commit).toHaveAttribute("href", `https://github.com/maycuatroi1/evo-agents/commit/${AGENT_COMMIT}`);
  await expect(commit).toHaveAttribute("target", "_blank");
  await expect(version.getByTestId("worker-updates")).toContainText("Runs the commit nightly points to.");
  // Its updates go as they should, and nothing on the page says so.
  await expect(main.getByTestId(/^worker-update-/)).toHaveCount(0);

  const settings = main.getByTestId("worker-settings");
  const toggle = settings.getByRole("switch", { name: "Lock version" });
  await expect(toggle).not.toBeChecked();
  await toggle.click();
  await expect(toggle).toBeChecked();
  await expect(toast(page, `${worker.name} stays on its version`)).toContainText("no longer updates itself to nightly");
  await expect(version.getByTestId("worker-updates")).toHaveText("Locked: it stays on this version until you unlock it in Settings.");
  expect(await lockedOf()).toBe(true);

  // The hub refuses (here it cannot write): said under the switch, which stays as the hub holds it.
  const patchOf = `**/v1/workers/${worker.id}`;
  await page.route(patchOf, (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill({ status: 503, json: { error: "service_unavailable", message: "the database is read-only" } })
      : route.fallback(),
  );
  await toggle.click();
  const refused = settings.getByTestId("worker-version-lock-error");
  await expect(refused).toContainText(`Couldn't unlock the version of ${worker.name}`);
  await expect(refused).toContainText("the database is read-only");
  await expect(toggle).toBeChecked();
  expect(await lockedOf()).toBe(true);

  // The next flip reaches the hub, and the refusal goes.
  await page.unroute(patchOf);
  await settings.getByTestId("worker-version-lock").getByText("Lock version").click(); // from its label
  await expect(toggle).not.toBeChecked();
  await expect(refused).toHaveCount(0);
  await expect(toast(page, `${worker.name} updates itself again`)).toContainText("installs nightly whenever it holds no run");
  expect(await lockedOf()).toBe(false);
});

test("the list and a worker's page say only what went wrong with its updates", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const named = (prefix: string) => registerWorker(me, { name: uniqueName(prefix), projects: [project] });
  const fine = await named("fine");
  const older = await named("older");
  const failed = await named("failed");
  const manual = await named("manual");
  const stuck = await named("stuck");
  const now = new Date().toISOString();
  // A wait of two hours is usual, and an older daemon says nothing of updates: neither shows anything.
  await heartbeat(fine.id, { agentCommit: AGENT_COMMIT, selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT }, selfUpdateHours: 2 });
  await heartbeat(older.id);
  await heartbeat(failed.id, {
    agentCommit: AGENT_COMMIT,
    selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT, error: "uv tool install exited 2: no matching distribution", error_at: now },
  });
  await heartbeat(manual.id, { selfUpdate: { state: "unsupported", reason: "not started by launchd or systemd" } });
  await heartbeat(stuck.id, { agentCommit: AGENT_COMMIT, selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT }, selfUpdateHours: 13 });

  await page.goto("/workers");
  await expect(row(page, failed.name).getByTestId("worker-update")).toHaveText("Update failed");
  await expect(row(page, failed.name).getByTestId("worker-update")).toHaveAttribute("data-problem", "failed");
  await expect(row(page, manual.name).getByTestId("worker-update")).toHaveText("Can't self-update");
  await expect(row(page, stuck.name).getByTestId("worker-update")).toHaveText("Update waiting");
  await expect(row(page, fine.name).getByTestId("worker-status")).toHaveText("Idle");
  await expect(row(page, fine.name).getByTestId("worker-update")).toHaveCount(0);
  await expect(row(page, older.name).getByTestId("worker-update")).toHaveCount(0);

  const main = page.locator("#main");
  await page.goto(`/workers/${failed.id}`);
  await expect(main.getByTestId("worker-update-failed")).toContainText("Its last update to nightly failed on");
  await expect(main.getByTestId("worker-update-error")).toHaveText("uv tool install exited 2: no matching distribution");
  await expect(main.getByTestId("worker-updates")).toContainText("Waiting for an idle moment to install def5678");
  await expect(main.getByTestId("worker-update-waiting")).toHaveCount(0); // its wait is short

  await page.goto(`/workers/${manual.id}`);
  await expect(main.getByTestId("worker-update-unsupported")).toContainText("not started by launchd or systemd");
  await expect(main.getByTestId("worker-commit")).toContainText("Not reported");

  await page.goto(`/workers/${older.id}`);
  await expect(main.getByTestId("worker-updates")).toContainText("its daemon does not update itself");
  await expect(main.getByTestId(/^worker-update-/)).toHaveCount(0);

  await page.goto(`/workers/${stuck.id}`);
  await expect(main.getByTestId("worker-update-waiting")).toContainText("drain it, then resume it once it shows the new commit");

  // Its owner locks it to keep it where it is: the note and the pill go.
  await main.getByRole("switch", { name: "Lock version" }).click();
  await expect(main.getByTestId("worker-update-waiting")).toHaveCount(0);
  await page.goto("/workers");
  await expect(row(page, stuck.name).getByTestId("worker-status")).toHaveText("Idle");
  await expect(row(page, stuck.name).getByTestId("worker-update")).toHaveCount(0);
  await expect(row(page, failed.name).getByTestId("worker-update")).toHaveText("Update failed");
});

test("its owner asks a worker behind nightly to update now, sees what it waits for, and cancels", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("behind"), projects: [me.projects[0]] });
  await heartbeat(worker.id, {
    agentCommit: AGENT_COMMIT,
    features: ["update_now"],
    selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT },
  });
  const updateNowOf = async () => (await workersOf(me)).find((w) => w.id === worker.id)?.update_now;

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  const panel = main.getByTestId("update-now");
  const ask = panel.getByRole("button", { name: "Update now" });
  await expect(ask).toHaveAccessibleDescription(/runs another commit than nightly/);
  await ask.click();
  await expect(toast(page, `${worker.name} updates now`)).toContainText("claims no new run");
  await expect(panel.getByTestId("update-now-status")).toHaveAttribute("data-phase", "asked");
  expect(await updateNowOf()).toMatchObject({ ended_at: null, outcome: null });

  // Its daemon took the request and holds no run: the page says it updates once its install is idle.
  const requestedAt = (await updateNowOf())?.requested_at ?? "";
  await heartbeat(worker.id, { selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT, update_now: { requested_at: requestedAt, ended: null } } });
  await page.reload();
  await expect(panel.getByTestId("update-now-status")).toHaveAttribute("data-phase", "starting");

  // The hub refuses the cancel (here it cannot write): said under the button, which stays as the hub holds it.
  const cancelOf = `**/v1/workers/${worker.id}/update-now/cancel`;
  await page.route(cancelOf, (route) => route.fulfill({ status: 503, json: { error: "service_unavailable", message: "the database is read-only" } }));
  const cancel = panel.getByRole("button", { name: "Cancel" });
  await cancel.click();
  const refused = panel.getByTestId("update-now-error");
  await expect(refused).toContainText(`Couldn't cancel the Update now of ${worker.name}`);
  await expect(refused).toContainText("the database is read-only");
  await page.unroute(cancelOf);

  await cancel.click();
  await expect(toast(page, `Update now of ${worker.name} cancelled`)).toContainText("claims runs again");
  await expect(panel.getByTestId("update-now-status")).toHaveCount(0);
  await expect(refused).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Update now" })).toBeVisible();
  expect(await updateNowOf()).toMatchObject({ outcome: "cancelled" });

  // Locked, the button is greyed with why.
  await main.getByRole("switch", { name: "Lock version" }).click();
  await expect(panel.getByTestId("update-now-hint")).toHaveAttribute("data-blocked", "locked");
  await expect(panel.getByRole("button", { name: "Update now" })).toHaveAttribute("aria-disabled", "true");
});

test("the Version card shows the commit nightly points to as the hub read it, in amber only once that read is old", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("reader"), projects: [me.projects[0]] });
  await heartbeat(worker.id, { agentCommit: NIGHTLY_COMMIT, selfUpdate: { state: "current", nightly_commit: NIGHTLY_COMMIT } });
  // The hub's read is one row for the whole hub, so the specs running beside this one keep theirs: the answer is edited.
  // The server renders the page from the hub's own answer, so the edited one shows from the browser's first refresh.
  test.slow();
  const REFRESH = { timeout: 15_000 };
  let readAt = new Date().toISOString();
  await page.route(`**/v1/workers/${worker.id}`, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), nightly: { commit: NIGHTLY_COMMIT, read_at: readAt } } });
  });

  await page.goto(`/workers/${worker.id}`);
  const version = page.locator("#main").getByTestId("worker-version");
  const read = version.getByTestId("worker-hub-nightly");
  await expect(read.getByRole("link", { name: "def5678 on GitHub (opens in a new tab)" })).toBeVisible(REFRESH);
  await expect(read.getByTestId("worker-hub-nightly-stale")).toHaveCount(0);
  await expect(version.getByTestId("update-now")).toHaveCount(0); // it runs nightly: nothing to offer

  readAt = new Date(Date.now() - 25 * 60 * 1000).toISOString();
  await page.reload();
  await expect(read.getByTestId("worker-hub-nightly-stale")).toContainText("the hub has not reached GitHub since", REFRESH);
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

test("the owner shares a worker from its Sharing card, which a hub admin does not get", async ({ page, member, signInAs }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("lent"), projects: [me.projects[0]] });
  await heartbeat(worker.id, { runtimes: RUNTIMES, checkouts: CHECKOUTS });

  await page.goto(`/workers/${worker.id}`);
  const main = page.locator("#main");
  const card = main.getByTestId("worker-sharing");
  await expect(card.getByRole("heading", { name: "Sharing" })).toBeVisible();
  // Nothing is wrong with a worker shared with no unit: no pill, and its switch is free.
  await expect(card.getByTestId("worker-doctor-none")).toBeVisible();
  await expect(card.getByTestId("worker-doctor-high")).toHaveCount(0);
  await expect(card.getByTestId("worker-shared-locked")).toHaveCount(0);
  await expect(card.getByTestId("worker-units-empty")).toBeVisible();
  const toggle = card.getByRole("switch", { name: "Share with the writers of its projects" });
  await expect(toggle).not.toBeChecked();
  await toggle.focus();
  await page.keyboard.press("Space");
  await expect(toast(page, `${worker.name} is shared`)).toBeVisible();
  await expect(toggle).toBeChecked();
  expect((await workersOf(me)).find((w) => w.id === worker.id)?.shared).toBe(true);

  await signOut(page);
  await signInAs(ADMIN_ACCOUNT);
  await page.goto(`/workers/${worker.id}`);
  await expect(main.getByTestId("worker-as-admin")).toBeVisible();
  await expect(main.getByTestId("worker-sharing")).toHaveCount(0);
});

test("its owner installs a browser on a worker from its page, sees it installing, cancels, and reads it installed", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: uniqueName("chromium"), projects: [me.projects[0]] });
  await heartbeat(worker.id, { runtimes: RUNTIMES, features: [] });
  const installOf = async () => (await workersOf(me)).find((w) => w.id === worker.id)?.browser_install;

  // An older daemon cannot install from the hub: the button is greyed with what to do instead.
  await page.goto(`/workers/${worker.id}`);
  const line = page.locator("#main").getByTestId("worker-browser");
  await expect(line).toHaveAttribute("data-offer", "blocked");
  await expect(line.getByTestId("worker-browser-hint")).toHaveAttribute("data-blocked", "true");
  await expect(line.getByTestId("worker-browser-detail")).toContainText("No Chromium");

  await heartbeat(worker.id, { features: ["browser_install"] });
  await page.reload();
  const ask = line.getByRole("button", { name: "Install browser" });
  await expect(ask).toHaveAccessibleDescription(/downloads Chromium/);
  await ask.click();
  await expect(toast(page, `${worker.name} installs a browser`)).toContainText("downloads Chromium");
  await expect(line.getByTestId("worker-browser-status")).toHaveAttribute("data-phase", "installing");
  expect(await installOf()).toMatchObject({ ended_at: null, outcome: null });

  await line.getByRole("button", { name: "Cancel" }).click();
  await expect(toast(page, `Install browser of ${worker.name} cancelled`)).toBeVisible();
  await expect(line.getByTestId("worker-browser-status")).toHaveCount(0);
  expect(await installOf()).toMatchObject({ outcome: "cancelled" });

  // Its heartbeat reports Chromium: the line says which, with no button.
  const chromium = { chromium: "153.0.8010.12", display: "headless", max: 2, sandbox: true };
  await heartbeat(worker.id, { features: ["browser_install", "browser"], browser: chromium });
  await page.reload();
  await expect(line).toHaveAttribute("data-offer", "installed");
  await expect(line.getByTestId("worker-browser-detail")).toHaveText("Chromium 153.0.8010.12, headless, 2 browsers at once, its own sandbox");
  await expect(line.getByRole("button")).toHaveCount(0);
});
