import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";

test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

test.describe("refusals from the API", () => {
  test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds users through the local stack");

  test("a 403 from the API shows the no-access state", async ({ page, member }) => {
    await member();
    // The API is what refuses: a member asking for the hub's admin data gets 403.
    const direct = await page.request.get("/v1/admin/stats");
    expect(direct.status()).toBe(403);
    expect((await direct.json()).error).toBe("forbidden");

    await page.goto("/admin");
    const state = page.getByTestId("state-forbidden");
    await expect(state).toBeVisible();
    await expect(state.getByRole("heading", { name: "Bạn không có quyền xem trang này" })).toBeVisible();
    await expect(state.getByRole("link", { name: "Về trang chủ" })).toHaveAttribute("href", "/");
    await expect(page.getByTestId("admin-stats")).toHaveCount(0);
    // The shell is still there around the state.
    await expect(page.getByTestId("user-menu")).toBeVisible();
  });

  test("a project the user holds no grant on is not found, as the API answers", async ({ page, member, admin }) => {
    await member();
    const hidden = uniqueName("hidden");
    await admin.registerProject(hidden);
    expect((await page.request.get(`/v1/projects/${hidden}`)).status()).toBe(404);
    await page.goto(`/p/${hidden}`);
    await expect(page.getByTestId("state-not-found")).toContainText(`Không tìm thấy dự án ${hidden}`);
  });

  test("an unknown page inside the shell is not found", async ({ page, member }) => {
    await member();
    await page.goto("/no/such/page");
    await expect(page.getByTestId("state-not-found")).toContainText("Không tìm thấy trang");
    await expect(page.getByTestId("user-menu")).toBeVisible();
  });
});
