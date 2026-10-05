import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type Account, type HubAdmin, newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { dispatch, liveWorker, RUN_PLAN, runRow, runsOf, seedRunPlan } from "./support/runs";
import { csrfHeader } from "./support/workers";

/**
 * Who may dispatch is the API's decision, not the page's: a reader sees the runs but no Dispatch or Run this step
 * button, and the API answers 403 when the reader posts a dispatch anyway; another writer cannot pin runs to my worker;
 * a hub admin without a grant sees no runs and cannot dispatch; someone without a grant gets 404. The calls go straight
 * to the API with the browser's session, CSRF header included.
 */
test.skip(isDeployed, "dispatches runs through the local stack");

const BODY = { plan_id: RUN_PLAN, steps: ["4"], runtime: "any", mode: "headless", approval: "review", timeout_min: 60 };

/** A project with the runs plan, a writer who owns a live worker on it and has dispatched step 2 to it. */
async function projectWithRuns(admin: HubAdmin) {
  const writer: Account = newAccount("writer");
  const project = uniqueName("runs");
  await admin.registerProject(project);
  await admin.grant(project, writer.login, "writer", "internal");
  await seedRunPlan(writer, project);
  const live = await liveWorker(writer, project, uniqueName("own"));
  const [run] = await dispatch(writer, project, ["2"]);
  return { writer, project, live, run };
}

test("a reader sees the runs but cannot dispatch, on the pages or through the API", async ({ page, admin, member }) => {
  const { writer, project, run } = await projectWithRuns(admin);
  const reader = await member([]);
  await admin.grant(project, reader.login, "reader", "internal");

  await open(page, `/p/${project}/runs`);
  await expect(runRow(page, run.id).getByTestId("run-state")).toHaveText("Queued");
  await expect(page.locator("#main").getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
  await expect(page.getByTestId("runs-dispatch")).toHaveCount(0);
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);

  await open(page, `/p/${project}/plans/${RUN_PLAN}/steps/4`);
  const section = page.locator("#main").getByTestId("step-runs");
  await expect(section.getByTestId("step-readiness")).toHaveText("Ready to run.");
  await expect(section).toContainText("Writers of this project can dispatch it");
  await expect(section.getByTestId("step-run")).toHaveCount(0);

  const headers = await csrfHeader(page);
  const refused = await page.request.post(`/v1/projects/${project}/runs`, { data: BODY, headers });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain("needs the writer role");
  // Without the CSRF header a cookie write is refused before anything else is looked at.
  const forged = await page.request.post(`/v1/projects/${project}/runs`, { data: BODY });
  expect(forged.status()).toBe(403);
  expect((await forged.json()).message).toContain("X-Evo-CSRF");
  expect((await runsOf(writer, project)).map((item) => item.id)).toEqual([run.id]);
});

test("another writer cannot pin runs to my worker, and the dialog offers only their own", async ({ page, admin, member }) => {
  const { writer, project, live } = await projectWithRuns(admin);
  const other = await member([]);
  await admin.grant(project, other.login, "writer", "internal");

  await open(page, `/p/${project}/runs`);
  await page.getByTestId("runs-dispatch").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await expect(dialog.getByTestId("dispatch-no-workers")).toBeVisible();
  await expect(dialog.getByTestId("dispatch-target-pin").getByRole("radio")).toBeDisabled();
  await expect(dialog).not.toContainText(live.worker.name);

  const headers = await csrfHeader(page);
  const refused = await page.request.post(`/v1/projects/${project}/runs`, {
    data: { ...BODY, worker_id: live.worker.id },
    headers,
  });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain("only to a worker of the member who dispatches it");
  expect(await runsOf(writer, project)).toHaveLength(1);
});

test("a hub admin without a grant sees no runs and cannot dispatch", async ({ page, admin, signInAs }) => {
  const { writer, project } = await projectWithRuns(admin);
  await signInAs(ADMIN_ACCOUNT);

  await open(page, `/p/${project}/runs`);
  await expect(page.locator("#main").getByTestId("state-forbidden")).toBeVisible();
  await expect(page.getByTestId("runs-dispatch")).toHaveCount(0);

  const headers = await csrfHeader(page);
  const listed = await page.request.get(`/v1/projects/${project}/runs`);
  expect(listed.status()).toBe(403);
  const refused = await page.request.post(`/v1/projects/${project}/runs`, { data: BODY, headers });
  expect(refused.status()).toBe(403);
  expect(await runsOf(writer, project)).toHaveLength(1);
});

test("someone without a grant on the project finds no runs", async ({ page, admin, member }) => {
  const { project, run } = await projectWithRuns(admin);
  await member([{ role: "writer", maxLevel: "internal" }]); // a writer elsewhere

  await open(page, `/p/${project}/runs`);
  await expect(page.locator("#main").getByTestId("state-not-found")).toBeVisible();
  const listed = await page.request.get(`/v1/projects/${project}/runs`);
  expect(listed.status()).toBe(404);
  const shown = await page.request.get(`/v1/projects/${project}/runs/${run.id}`);
  expect(shown.status()).toBe(404);
  const refused = await page.request.post(`/v1/projects/${project}/runs`, { data: BODY, headers: await csrfHeader(page) });
  expect(refused.status()).toBe(404);
});
