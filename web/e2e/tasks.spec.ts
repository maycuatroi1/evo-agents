import type { Page } from "@playwright/test";

import { signOut } from "./support/auth";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { dispatch, seedRunPlan } from "./support/runs";
import { enableSrs, enableTasks, importSample, itemPath, RULE, USE_CASE } from "./support/srs";
import {
  boardColumn,
  createSprintByApi,
  createTaskByApi,
  editTaskByApi,
  SAMPLE_TASKS,
  taskCard,
  taskOf,
  taskPath,
  tasksPath,
} from "./support/tasks";
import { toast } from "./support/toast";

/**
 * Criterion a6 of plan srs-tasks-plugins in the browser, against the real API: a project that enabled the core plugin
 * tasks has Tasks in its menu, the board of a sprint by status, the backlog by sprint and the visitor's own tasks; a
 * writer moves and assigns a task from the board (its menus, or dragging it), plans and closes sprints, and reads and
 * changes a task whole with its links to a use case, a plan step, a run and a pull request; an SRS use case's page
 * creates a task linked to it; JSON imports after the hub's dry run; a reader only reads; a project that has not
 * enabled it has no menu item, and its pages and API answer 404.
 */
test.skip(isDeployed, "enables plugins and seeds tasks through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("Tasks shows only where a project enabled it: no menu item, and its pages and API answer 404 elsewhere", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const api = await apiOf(me);
  await page.goto(tasksPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(taskPath(project, "T-1"));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-tasks-board")).toHaveCount(0);
  const off = await api.GET("/v1/x/tasks/{project}/tasks", { params: { path: { project } } });
  expect(off.response.status).toBe(404);

  await enableTasks(me, project);
  await page.reload();
  await page.getByTestId("nav-plugin-tasks-board").click();
  await expect(page).toHaveURL(tasksPath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Tasks");
  await expect(main(page).getByTestId("tasks-board-empty")).toBeVisible();
  const on = await api.GET("/v1/x/tasks/{project}/tasks", { params: { path: { project } } });
  expect(on.response.status).toBe(200);
});

test("a writer starts a sprint, adds a task from its board, then moves it by its menu and by dragging, and assigns it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const bao = newAccount("tasks-bao");
  await new HubAdmin().grant(project, bao.login, "writer", "internal");
  await enableTasks(me, project);

  // A sprint, started at once from the backlog's dialog.
  await page.goto(tasksPath(project, "backlog"));
  await main(page).getByTestId("sprint-new").click();
  const dialog = page.getByTestId("sprint-dialog");
  await dialog.getByTestId("sprint-create").click();
  await expect(dialog.getByText("Give the sprint a name.")).toBeVisible();
  await dialog.getByTestId("sprint-name").fill("Sprint 7");
  await dialog.getByRole("switch", { name: "Start it now" }).click();
  await dialog.getByTestId("sprint-create").click();
  await expect(toast(page, "Started Sprint 7")).toBeVisible();
  await expect(main(page).getByTestId("backlog-sprint").first()).toContainText("Sprint 7");
  await expect(main(page).getByTestId("backlog-sprint").first().getByTestId("sprint-state")).toHaveText("Active");

  // The board opens on it; a new task from there joins it.
  await main(page).getByTestId("tasks-tab-board").click();
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Sprint 7");
  await main(page).getByTestId("tasks-new").click();
  await expect(page).toHaveURL(`${tasksPath(project, "board/new")}?sprint=Sprint+7`);
  await expect(main(page).getByTestId("task-new-sprint")).toHaveValue("Sprint 7");
  await main(page).getByTestId("task-new-title").fill("Cancel an order before it is packed");
  await main(page).getByTestId("task-new-create").click();
  await expect(toast(page, "Created T-1")).toBeVisible();
  await expect(page).toHaveURL(taskPath(project, "T-1"));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Cancel an order before it is packed");

  // Its menus on the board move it and assign it, at once and for the hub.
  await page.goto(tasksPath(project));
  await expect(boardColumn(page, "backlog").getByTestId("task-card")).toHaveCount(1);
  await taskCard(page, "T-1").getByTestId("task-status-select").selectOption("in_progress");
  await expect(toast(page, "Moved T-1 to In progress")).toBeVisible();
  await expect(boardColumn(page, "in_progress").getByTestId("task-card")).toHaveAttribute("data-task", "T-1");
  await taskCard(page, "T-1").getByTestId("task-assignee-select").selectOption(bao.login);
  await expect(toast(page, `Assigned T-1 to ${bao.login}`)).toBeVisible();
  const held = await taskOf(me, project, "T-1");
  expect([held.status, held.assignees]).toEqual(["in_progress", [bao.login]]);

  // Dragging a card to another column moves it too.
  await createTaskByApi(me, project, { title: "Test cases for the cancel window", status: "todo", sprint: "Sprint 7", priority: "high", assignees: [me.login] });
  await page.reload();
  await taskCard(page, "T-2").dragTo(boardColumn(page, "in_review"));
  await expect(toast(page, "Moved T-2 to In review")).toBeVisible();
  await expect(boardColumn(page, "in_review").getByTestId("task-card")).toHaveAttribute("data-task", "T-2");
  expect((await taskOf(me, project, "T-2")).status).toBe("in_review");

  // Only mine keeps the visitor's own; My work lists them.
  await main(page).getByTestId("board-mine").click();
  await expect(main(page).getByTestId("task-card")).toHaveCount(1);
  await main(page).getByTestId("tasks-tab-mine").click();
  await expect(main(page).getByTestId("mine-open-table")).toContainText("Test cases for the cancel window");
  await expect(main(page).getByTestId("mine-open-table")).not.toContainText("Cancel an order before it is packed");
});

test("the backlog moves a task out of its sprint and closes the sprint, moving what is unfinished to the next one", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableTasks(me, project);
  await createSprintByApi(me, project, "Sprint 7", "active");
  await createSprintByApi(me, project, "Sprint 8");
  await createTaskByApi(me, project, { title: "Hold stock at checkout", sprint: "Sprint 7", status: "done", priority: "medium" });
  await createTaskByApi(me, project, { title: "Payment retries", sprint: "Sprint 7", status: "in_progress", priority: "medium" });
  await createTaskByApi(me, project, { title: "Parcel tracking", sprint: "Sprint 7", status: "todo", priority: "low" });

  await page.goto(tasksPath(project, "backlog"));
  const sprint7 = main(page).getByTestId("backlog-sprint").filter({ hasText: "Sprint 7" });
  await expect(sprint7.getByTestId("tasks-table").locator("tbody tr")).toHaveCount(3);
  await sprint7.getByRole("combobox", { name: "Sprint of T-3" }).selectOption("");
  await expect(toast(page, "Took T-3 out of its sprint")).toBeVisible();
  await expect(main(page).getByTestId("backlog-none")).toContainText("Parcel tracking");

  await sprint7.getByTestId("sprint-close").click();
  const dialog = page.getByTestId("sprint-dialog");
  await expect(dialog.getByTestId("sprint-move-to")).toHaveValue("Sprint 8");
  await dialog.getByTestId("sprint-close-confirm").click();
  await expect(toast(page, "Closed Sprint 7")).toBeVisible();
  expect((await taskOf(me, project, "T-2")).sprint).toBe("Sprint 8");
  expect((await taskOf(me, project, "T-1")).sprint).toBe("Sprint 7");
  await expect(main(page).getByTestId("backlog-closed")).toContainText("1 closed sprint");
  await expect(main(page).getByTestId("sprint-start")).toBeVisible();
});

test("a task's page links a use case, a plan step, a run and a pull request; a writer edits, links, unlinks and comments", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableSrs(me, project);
  await enableTasks(me, project);
  await importSample(me, project);
  await seedRunPlan(me, project);
  const [run] = await dispatch(me, project, ["2"]);
  const pull = "https://github.com/example-org/api/pull/7";
  await createTaskByApi(me, project, {
    title: "Checkout places the order",
    priority: "medium",
    description_md: "Customers place an order from the cart.",
    links: { use_cases: [USE_CASE.code], steps: ["rollout#2"], runs: [run.id], pulls: [pull] },
  });

  await page.goto(taskPath(project, "T-1"));
  const links = main(page).getByTestId("task-links");
  await expect(links.getByTestId("task-links-srs").getByRole("link", { name: USE_CASE.code })).toHaveAttribute(
    "href",
    `/p/${project}/x/srs/documents/demo-shop/items/${encodeURIComponent(USE_CASE.code)}`,
  );
  await expect(links.getByTestId("task-links-srs")).toContainText(USE_CASE.title);
  await expect(links.getByTestId("task-links-steps").getByRole("link", { name: "rollout#2" })).toHaveAttribute("href", `/p/${project}/plans/rollout/steps/2`);
  await expect(links.getByTestId("task-links-runs")).toContainText(`#${run.id}`);
  await expect(links.getByTestId("task-links-pulls")).toContainText("example-org/api#7");

  // Edit the description, add a business rule, drop the pull request, comment.
  await main(page).getByTestId("task-edit").click();
  await main(page).getByTestId("task-editor-body").fill("Customers place an order from the cart.\n\nThe cart is kept for a day.");
  await main(page).getByTestId("task-editor-save").click();
  await expect(toast(page, "Saved T-1")).toBeVisible();
  await expect(main(page).getByTestId("task-description-body")).toContainText("The cart is kept for a day.");
  await links.getByTestId("task-link-input").fill(`${RULE.code}, nope#1#2`);
  await links.getByTestId("task-link-add").click();
  await expect(links.getByText("Not a link: nope#1#2")).toBeVisible();
  await links.getByTestId("task-link-input").fill(RULE.code);
  await links.getByTestId("task-link-add").click();
  await expect(toast(page, "Linked 1 item to T-1")).toBeVisible();
  await expect(links.getByTestId("task-links-srs")).toContainText(RULE.title);
  await links.getByRole("button", { name: "Remove the link to example-org/api#7" }).click();
  await expect(toast(page, "Removed the link to example-org/api#7 from T-1")).toBeVisible();
  await expect(links.getByTestId("task-links-pulls")).toContainText("None");
  await main(page).getByTestId("task-comment-body").fill("Is a day long enough?");
  await main(page).getByTestId("task-comment-submit").click();
  await expect(toast(page, "Comment added")).toBeVisible();
  await expect(main(page).getByTestId("task-comment")).toContainText("Is a day long enough?");

  // Details change where they stand; the history says what happened.
  await main(page).getByTestId("task-priority").selectOption("high");
  await expect(toast(page, "T-1 is High now")).toBeVisible();
  await expect(main(page).getByTestId("task-head").locator("[data-priority]")).toHaveText("High");
  await main(page).getByTestId("task-people-ba-add").selectOption(me.login);
  await expect(main(page).getByTestId("task-people-ba")).toContainText(me.login);
  const history = main(page).getByTestId("task-history");
  await expect(history).toContainText(`linked the SRS item ${RULE.code}`);
  await expect(history).toContainText("changed the description");
  await expect(history).toContainText("removed a pull request link");

  // The use case opens on its page.
  await links.getByTestId("task-links-srs").getByRole("link", { name: USE_CASE.code }).click();
  await expect(page).toHaveURL(itemPath(project, USE_CASE.code));
});

test("an SRS item's page creates a task linked to it, for a writer of a project that enabled tasks only", async ({ page, member, signInAs }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const reader = newAccount("tasks-reader");
  await new HubAdmin().grant(project, reader.login, "reader", "internal");
  await enableSrs(me, project);
  await importSample(me, project);

  await page.goto(itemPath(project, USE_CASE.code));
  await expect(main(page).getByTestId("srs-item-code")).toHaveText(USE_CASE.code);
  await expect(main(page).getByTestId("srs-item-create-task")).toHaveCount(0);

  await enableTasks(me, project);
  await page.reload();
  await main(page).getByTestId("srs-item-create-task").click();
  await expect(page).toHaveURL(`${tasksPath(project, "board/new")}?link=${encodeURIComponent(USE_CASE.code).replace(/%20/g, "+")}`);
  await expect(main(page).getByTestId("task-new-links")).toHaveValue(USE_CASE.code);
  await main(page).getByTestId("task-new-title").fill("Place an order with a card");
  await main(page).getByTestId("task-new-create").click();
  await expect(toast(page, "Created T-1")).toBeVisible();
  await expect(main(page).getByTestId("task-links-srs")).toContainText(USE_CASE.code);

  await signOut(page);
  await signInAs(reader);
  await page.goto(itemPath(project, USE_CASE.code));
  await expect(main(page).getByTestId("srs-item-code")).toHaveText(USE_CASE.code);
  await expect(main(page).getByTestId("srs-item-create-task")).toHaveCount(0);
});

test("a writer blocks a task by hand and clears it; Blocked shows on the board, the backlog, My work and the task, and a reader only reads it", async ({ page, member, signInAs }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const reader = newAccount("tasks-blocked-reader");
  await new HubAdmin().grant(project, reader.login, "reader", "internal");
  await enableTasks(me, project);
  await createSprintByApi(me, project, "Sprint 7", "active");
  await createTaskByApi(me, project, { title: "Payment retries", sprint: "Sprint 7", status: "todo", assignees: [me.login] });
  await createTaskByApi(me, project, { title: "Cart API", sprint: "Sprint 7", status: "todo", assignees: [me.login] });

  await page.goto(taskPath(project, "T-1"));
  const panel = main(page).getByTestId("task-blockers");
  await expect(panel.getByTestId("task-blockers-empty")).toBeVisible();
  await expect(main(page).getByTestId("task-head").getByTestId("task-blocked")).toHaveCount(0);
  await panel.getByTestId("task-blocker-reason-input").fill("Needs the cart API");
  await panel.getByTestId("task-blocker-kind").selectOption("task");
  await panel.getByTestId("task-blocker-value").fill("T-2");
  await panel.getByTestId("task-blocker-add").click();
  await expect(toast(page, "Blocked T-1")).toBeVisible();
  const line = panel.getByTestId("task-blocker");
  await expect(line.getByTestId("task-blocker-reason")).toHaveText("Needs the cart API");
  await expect(line.getByTestId("task-blocker-task")).toHaveAttribute("href", taskPath(project, "T-2"));
  await expect(line).toContainText(`set by ${me.login}`);
  await expect(main(page).getByTestId("task-head").getByTestId("task-blocked")).toBeVisible();

  // A task that is not another task of the project is refused, and the form says so.
  await panel.getByTestId("task-blocker-reason-input").fill("Typo");
  await panel.getByTestId("task-blocker-value").fill("T-99");
  await panel.getByTestId("task-blocker-add").click();
  await expect(panel.getByRole("alert")).toBeVisible();

  await page.goto(tasksPath(project));
  await expect(taskCard(page, "T-1").getByTestId("task-blocked")).toBeVisible();
  await expect(taskCard(page, "T-2").getByTestId("task-blocked")).toHaveCount(0);
  await page.goto(tasksPath(project, "backlog"));
  await expect(main(page).getByTestId("task-blocked")).toHaveCount(1);
  await page.goto(tasksPath(project, "mine"));
  await expect(main(page).getByTestId("task-blocked")).toHaveCount(1);

  await signOut(page);
  await signInAs(reader);
  await page.goto(taskPath(project, "T-1"));
  await expect(main(page).getByTestId("task-blocker")).toContainText("Needs the cart API");
  await expect(main(page).getByTestId("task-blocker-form")).toHaveCount(0);
  await expect(main(page).getByTestId("task-blocker-clear")).toHaveCount(0);
  await signOut(page);

  await signInAs(me);
  await page.goto(taskPath(project, "T-1"));
  await main(page).getByTestId("task-blocker-clear").click();
  await expect(toast(page, "Cleared a blocker of T-1")).toBeVisible();
  await expect(main(page).getByTestId("task-blockers-empty")).toBeVisible();
  await expect(main(page).getByTestId("task-blocked")).toHaveCount(0);
  await page.goto(tasksPath(project));
  await expect(taskCard(page, "T-1")).toBeVisible();
  await expect(main(page).getByTestId("task-blocked")).toHaveCount(0);
});

test("a reader reads the board, the backlog and a task, with no control that writes; creating and importing are a writer's", async ({ page, signInAs }) => {
  const owner = newAccount("tasks-owner");
  const reader = newAccount("tasks-reader");
  const hub = new HubAdmin();
  const project = uniqueName("tasks");
  await hub.registerProject(project);
  await hub.grant(project, owner.login, "admin", "internal");
  await hub.grant(project, reader.login, "reader", "internal");
  await enableTasks(owner, project);
  await createSprintByApi(owner, project, "Sprint 7", "active");
  await createTaskByApi(owner, project, { title: "Payment retries", sprint: "Sprint 7", status: "todo", priority: "high", assignees: [reader.login] });
  await editTaskByApi(owner, project, "T-1", { roles: { qa: [owner.login] } });

  await signInAs(reader);
  await page.goto(tasksPath(project));
  await expect(main(page).getByTestId("tasks-read-only")).toBeVisible();
  await expect(taskCard(page, "T-1")).toBeVisible();
  await expect(main(page).getByTestId("task-status-select")).toHaveCount(0);
  await expect(main(page).getByTestId("tasks-new")).toHaveCount(0);
  await expect(main(page).getByTestId("tasks-tab-import")).toHaveCount(0);
  await page.goto(tasksPath(project, "backlog"));
  await expect(main(page).getByTestId("sprint-new")).toHaveCount(0);
  await expect(main(page).getByTestId("task-sprint-select")).toHaveCount(0);
  await page.goto(taskPath(project, "T-1"));
  await expect(main(page).getByTestId("task-people-qa")).toContainText(owner.login);
  await expect(main(page).getByTestId("task-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("task-link-form")).toHaveCount(0);
  await expect(main(page).getByTestId("task-comment-form")).toHaveCount(0);
  await expect(main(page).getByTestId("task-page-status")).toHaveCount(0);
  await page.goto(tasksPath(project, "mine"));
  await expect(main(page).getByTestId("mine-open-table")).toContainText("Payment retries");
  await page.goto(tasksPath(project, "board/new"));
  await expect(main(page).getByTestId("task-new-reader")).toBeVisible();
  await page.goto(tasksPath(project, "import"));
  await expect(main(page).getByTestId("tasks-import-reader")).toBeVisible();
  await page.goto(taskPath(project, "T-9"));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
});

test("a writer imports evo-tasks/1 JSON after the hub's dry run, which an unknown status stops", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableSrs(me, project);
  await enableTasks(me, project);
  await importSample(me, project);

  await page.goto(tasksPath(project));
  await main(page).getByTestId("tasks-tab-import").click();
  await expect(page).toHaveURL(tasksPath(project, "import"));
  await expect(main(page).getByTestId("tasks-import-preview-run")).toBeDisabled();
  await main(page).getByTestId("tasks-import-json").fill("{ not json");
  await expect(main(page).getByText(/^Not JSON/)).toBeVisible();
  const broken = JSON.parse(SAMPLE_TASKS) as { status_map: Record<string, string> };
  delete broken.status_map.Doing;
  await main(page).getByTestId("tasks-import-json").fill(JSON.stringify(broken));
  await main(page).getByTestId("tasks-import-preview-run").click();
  const preview = main(page).getByTestId("tasks-import-preview");
  await expect(preview.getByTestId("tasks-import-verdict")).toHaveText("1 error stops the import");
  await expect(main(page).getByTestId("tasks-import-run")).toBeDisabled();

  await main(page).getByTestId("tasks-import-json").fill(SAMPLE_TASKS);
  await expect(main(page).getByTestId("tasks-import-run")).toBeDisabled();
  await main(page).getByTestId("tasks-import-preview-run").click();
  await expect(preview.getByTestId("tasks-import-verdict")).toHaveText("Ready to import");
  await expect(preview.getByTestId("tasks-import-counts")).toContainText("3 tasks");
  await expect(preview.getByTestId("tasks-import-problems")).toContainText("UC SHIP-1 is not linked");
  await main(page).getByTestId("tasks-import-run").click();
  await expect(toast(page, "Imported 3 tasks")).toBeVisible();
  await expect(page).toHaveURL(tasksPath(project, "backlog"));
  await expect(main(page).getByTestId("backlog-sprint").filter({ hasText: "Sprint 7" })).toContainText("Place an order with a card");
  await page.goto(taskPath(project, "T-1"));
  await expect(main(page).getByTestId("task-head")).toContainText("Imported from notion 0000aaaa");
  await expect(main(page).getByTestId("task-links-srs")).toContainText(USE_CASE.code);
});
