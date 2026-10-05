import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount } from "./support/hub";
import { kgPath, nodePath, open, RUNBOOK, sharedKg } from "./support/kg";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

/**
 * Who may not read a project's knowledge graph: a user without a grant on the project. A hub admin sees that the
 * project exists, so the API answers 403 and the pages show the no-access state; anyone else is told nothing about
 * the project, as for one that does not exist (404).
 */
test.describe("knowledge graph refusals", () => {
  test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds a graph through the local stack");

  test("a user without a grant sees the no-access page and the API answers 403", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    await signInAs(ADMIN_ACCOUNT); // the hub admin holds no grant on the project
    for (const path of ["graph", "builds", "nodes?q=runbook", `node?id=${encodeURIComponent(RUNBOOK)}`]) {
      const response = await page.request.get(`/v1/kg/${kg.project}/${path}`);
      expect(response.status(), path).toBe(403);
      expect((await response.json()).error).toBe("forbidden");
    }
    for (const path of [kgPath(kg.project), `${kgPath(kg.project)}?q=runbook`, nodePath(kg.project, RUNBOOK)]) {
      await open(page, path);
      const state = page.getByTestId("state-forbidden");
      await expect(state, path).toBeVisible();
      await expect(state.getByRole("heading", { name: "Bạn không có quyền xem trang này" })).toBeVisible();
      await expect(page.getByTestId("kg-neighbours")).toHaveCount(0);
      await expect(page.getByTestId("kg-latest-build")).toHaveCount(0);
      await expect(page.getByTestId("user-menu")).toBeVisible(); // the shell stays around the state
    }
  });

  test("a member of other projects is told nothing about this one (404)", async ({ page, signInAs }) => {
    const kg = await sharedKg();
    await signInAs(newAccount("kg-outsider"));
    expect((await page.request.get(`/v1/kg/${kg.project}/graph`)).status()).toBe(404);
    expect((await page.request.get(`/v1/kg/${kg.project}/node?id=${encodeURIComponent(RUNBOOK)}`)).status()).toBe(404);
    await open(page, kgPath(kg.project));
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy dự án ${kg.project}`);
    await open(page, nodePath(kg.project, RUNBOOK));
    await expect(page.getByTestId("state-not-found")).toBeVisible();
  });

  test("a visitor without a session is sent to sign in", async ({ page }) => {
    const kg = await sharedKg();
    expect((await page.request.get(`/v1/kg/${kg.project}/graph`)).status()).toBe(401);
    await open(page, kgPath(kg.project));
    await page.waitForURL((url) => url.pathname === "/login");
  });
});
