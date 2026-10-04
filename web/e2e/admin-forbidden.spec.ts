import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";

/**
 * The admin area belongs to hub admins because the API says so, not because the web hides a menu: a reader opening
 * any admin page sees the no-access state, every admin route answers 403 to their session, and a cookie write
 * without the session's X-Evo-CSRF header is refused even for an admin.
 */
test.skip(isDeployed, "seeds users through the local stack");

const PAGES = ["/admin", "/admin/members", "/admin/members/e2e-admin", "/admin/tokens", "/admin/audit?actor=e2e-admin"];

test("a reader opening the admin area sees the no-access state on every page", async ({ page, member }) => {
  await member([{ role: "reader", maxLevel: "internal" }]);
  await expect(page.getByRole("navigation", { name: "Điều hướng chính" }).getByRole("link", { name: "Quản trị" })).toHaveCount(0);
  for (const path of PAGES) {
    await page.goto(path);
    const state = page.getByTestId("state-forbidden");
    await expect(state, path).toBeVisible();
    await expect(state.getByRole("heading", { name: "Bạn không có quyền xem trang này" })).toBeVisible();
    await expect(page.getByTestId("admin-nav")).toHaveCount(0); // the admin tabs are not offered either
    await expect(page.getByTestId("members-table")).toHaveCount(0);
    await expect(page.getByTestId("tokens-table")).toHaveCount(0);
    await expect(page.getByTestId("audit-table")).toHaveCount(0);
  }
});

test("a reader calling each admin API directly gets 403, writes included", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const csrf = await page.request.get("/v1/auth/web/csrf");
  expect(csrf.status()).toBe(200);
  const header = { "X-Evo-CSRF": (await csrf.json()).csrf as string };
  const calls: { method: "GET" | "PUT" | "DELETE"; path: string; data?: unknown }[] = [
    { method: "GET", path: "/v1/admin/users" },
    { method: "GET", path: "/v1/admin/stats" },
    { method: "GET", path: "/v1/admin/audit" },
    { method: "GET", path: `/v1/admin/audit?actor=${me.login}` },
    { method: "GET", path: "/v1/admin/audit/actions" },
    { method: "GET", path: "/v1/admin/tokens" },
    { method: "GET", path: "/v1/admin/tokens?state=any" },
    { method: "PUT", path: `/v1/admin/projects/${project}/grants/${me.login}`, data: { role: "admin", max_level: "secret" } },
    { method: "DELETE", path: `/v1/admin/projects/${project}/grants/${me.login}` },
    { method: "DELETE", path: "/v1/admin/tokens/1" },
  ];
  for (const { method, path, data } of calls) {
    const response = await page.request.fetch(path, { method, data, headers: header });
    expect(response.status(), `${method} ${path}`).toBe(403);
    const body = await response.json();
    expect(body.error).toBe("forbidden");
    expect(body.message).toContain("EVO_HUB_ADMINS");
  }
  // Nothing changed: still a reader at the same level.
  const projects = await (await page.request.get("/v1/projects")).json();
  expect(projects).toEqual([expect.objectContaining({ name: project, role: "reader", max_level: "internal" })]);
});

test("a grant made with an admin's cookie but without X-Evo-CSRF is refused with 403", async ({ page, admin, signInAs }) => {
  const project = uniqueName("csrf");
  await admin.registerProject(project);
  const target = newAccount("csrf-target");
  await signInAs(ADMIN_ACCOUNT);
  const path = `/v1/admin/projects/${project}/grants/${target.login}`;
  const body = { role: "reader", max_level: "public" };

  const attempts: Record<string, string>[] = [{}, { "X-Evo-CSRF": "forged-value" }];
  for (const headers of attempts) {
    const refused = await page.request.put(path, { data: body, headers });
    expect(refused.status()).toBe(403);
    expect((await refused.json()).message).toContain("X-Evo-CSRF");
  }
  const users: { login: string }[] = await (await page.request.get("/v1/admin/users")).json();
  expect(users.map((user) => user.login)).not.toContain(target.login); // nothing was granted

  const csrf = (await (await page.request.get("/v1/auth/web/csrf")).json()).csrf as string;
  const granted = await page.request.put(path, { data: body, headers: { "X-Evo-CSRF": csrf } });
  expect(granted.status()).toBe(200);
  expect(await granted.json()).toMatchObject({ project, login: target.login, role: "reader", created: true });
  const revoke = await page.request.delete(path);
  expect(revoke.status()).toBe(403); // a revoke is a write too
  expect((await page.request.delete(path, { headers: { "X-Evo-CSRF": csrf } })).status()).toBe(204);
});
