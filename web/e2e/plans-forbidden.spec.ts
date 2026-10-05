import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * Who may not read a project's plans, decided by the API and only shown by the pages. A hub admin without a grant
 * on the project knows the project exists, so the API answers 403 and the page says there is no access. Anyone
 * else without a grant gets the 404 of a project they cannot see, and a member whose level stops below a plan's
 * label gets the same 404 as for a plan that does not exist.
 */
test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds users and plans through the local stack");

test.describe("plans refused by the API", () => {
  test("a user without a grant on the project sees the no-access page, and the API answers 403", async ({
    page,
    admin,
    signInAs,
  }) => {
    const project = uniqueName("plans");
    const writer = newAccount("writer");
    await admin.registerProject(project);
    await admin.grant(project, writer.login, "writer", "internal");
    await seedPlans(writer, project);

    await signInAs(ADMIN_ACCOUNT); // a hub admin, with no grant on this project
    for (const path of ["", `/${ACTIVE_PLAN}`, `/${ACTIVE_PLAN}/revisions`, `/${ACTIVE_PLAN}/diff?from=1&to=2`]) {
      const direct = await page.request.get(`/v1/projects/${project}/plans${path}`);
      expect(direct.status(), path).toBe(403);
      expect((await direct.json()).error).toBe("forbidden");
    }
    for (const path of ["", `/${ACTIVE_PLAN}`, `/${ACTIVE_PLAN}/steps/10`, `/${ACTIVE_PLAN}/revisions?from=1&to=2`]) {
      await open(page, `/p/${project}/plans${path}`);
      const state = page.getByTestId("state-forbidden");
      await expect(state, path).toBeVisible();
      await expect(state.getByRole("heading", { name: "Bạn không có quyền xem trang này" })).toBeVisible();
      await expect(page.getByTestId("plan-progress")).toHaveCount(0);
      await expect(page.getByTestId("step-evidence")).toHaveCount(0);
      await expect(page.getByTestId("user-menu")).toBeVisible();
    }
  });

  test("a member of another project gets the 404 of a project it cannot see", async ({ page, member, admin }) => {
    await member();
    const project = uniqueName("plans");
    const writer = newAccount("writer");
    await admin.registerProject(project);
    await admin.grant(project, writer.login, "writer", "internal");
    await seedPlans(writer, project);

    expect((await page.request.get(`/v1/projects/${project}/plans`)).status()).toBe(404);
    expect((await page.request.get(`/v1/projects/${project}/plans/${ACTIVE_PLAN}`)).status()).toBe(404);
    await open(page, `/p/${project}/plans`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy dự án ${project}`);
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/steps/10`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy plan ${ACTIVE_PLAN}`);
    await expect(page.getByTestId("step-evidence")).toHaveCount(0);
  });

  test("a member whose level stops below the plan's label sees no plan, as for one that does not exist", async ({
    page,
    member,
    admin,
  }) => {
    const me = await member([{ role: "reader", maxLevel: "public" }]);
    const project = me.projects[0];
    const writer = newAccount("writer");
    await admin.grant(project, writer.login, "writer", "internal");
    await seedPlans(writer, project); // labelled internal, the project's default

    const hidden = await page.request.get(`/v1/projects/${project}/plans/${ACTIVE_PLAN}`);
    const missing = await page.request.get(`/v1/projects/${project}/plans/no-such-plan`);
    expect([hidden.status(), missing.status()]).toEqual([404, 404]);
    expect(await (await page.request.get(`/v1/projects/${project}/plans`)).json()).toEqual([]);

    await open(page, `/p/${project}/plans`);
    await expect(page.getByTestId("state-empty")).toContainText("Dự án chưa có plan nào trên hub");
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy plan ${ACTIVE_PLAN}`);
    await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/revisions`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy plan ${ACTIVE_PLAN}`);
  });

  test("a name that cannot be a plan is not found without asking the API", async ({ page, member }) => {
    const me = await member();
    await open(page, `/p/${me.projects[0]}/plans/Not_A_Plan`);
    await expect(page.getByTestId("state-not-found")).toContainText("Không tìm thấy plan Not_A_Plan");
  });
});
