import { readFile } from "node:fs/promises";

import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, type HubAdmin, newAccount, registration, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { type Bundle, packSkill, publishSkill, sha256 } from "./support/skills";

/**
 * Skills on the web, against the real API and the stack's S3: a member sees the project's skills and the global
 * ones, every version with its SHA-256, size and source commit on GitHub, and downloads a bundle by navigating to
 * the presigned URL the API hands out. The bytes saved hash to what the hub recorded.
 *
 * Locators stay inside #main: while a page streams in, React keeps a hidden copy of a segment outside it for a
 * moment, which a page-wide test id would also match.
 */
test.skip(isDeployed, "publishes skills through the local stack");

const COMMIT = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432";

async function seeded(admin: HubAdmin) {
  const project = uniqueName("skills");
  await admin.registerProject(
    project,
    registration({
      repos: [
        {
          name: "agent-skills",
          origin: "git@github.com:example-org/agent-skills.git",
          default_branch: "main",
          path: "agent-skills",
        },
      ],
    }),
  );
  const writer = newAccount("writer");
  const reader = newAccount("reader");
  await admin.grant(project, writer.login, "writer", "internal");
  await admin.grant(project, reader.login, "reader", "public");
  const writerApi = await apiOf(writer);
  const first = await packSkill("team-notes", "First version.", "Use when writing team notes");
  const second = await packSkill("team-notes", "Second version, with more steps.", "Use when writing team notes");
  await publishSkill(writerApi, first, "team-notes", project, { repo: "agent-skills", commit: "0123abc" });
  await publishSkill(writerApi, second, "team-notes", project, { repo: "example-org/agent-skills", commit: COMMIT });
  const global = uniqueName("house-style");
  const house = await packSkill(global, "Write like the team.", "Use when writing anything");
  await publishSkill(await apiOf(ADMIN_ACCOUNT), house, global, null);
  return { project, reader, first, second, global, house };
}

test("a member reads the project's skills and the shared ones, with every version, its SHA-256, size and source", async ({
  page,
  admin,
  signInAs,
}) => {
  const { project, reader, first, second, global } = await seeded(admin);
  await signInAs(reader);

  await page.goto(`/p/${project}/skills`);
  const table = page.locator("#main").getByTestId("skills-table");
  await expect(table).toBeVisible();
  await expect(table.locator("[data-skill-name]")).toHaveText(["team-notes"]);
  await expect(table).toContainText("v2");
  await expect(table.getByTestId("source-link")).toHaveAttribute(
    "href",
    `https://github.com/example-org/agent-skills/commit/${COMMIT}`,
  );
  await expect(table).not.toContainText(global);

  await page.goto("/skills");
  await expect(page.locator("#main").getByTestId("skills-table").locator(`[data-skill-name="${global}"]`)).toBeVisible();
  await expect(page.locator("#main").getByTestId("skills-table").locator('[data-skill-name="team-notes"]')).toHaveCount(0);
  await page.locator("#main").getByTestId("skills-filter").fill(global);
  await expect(page.locator("#main").getByTestId("skills-table").locator("[data-skill-name]")).toHaveText([global]);

  await page.goto(`/p/${project}/skills/team-notes`);
  const versions = page.locator("#main").getByTestId("skill-versions");
  await expect(versions).toBeVisible();
  const rows = versions.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("v2");
  await expect(rows.nth(0).locator(`[data-sha256="${second.sha256}"]`).first()).toBeAttached();
  await expect(rows.nth(1).locator(`[data-sha256="${first.sha256}"]`).first()).toBeAttached();
  // v1 named only the repo: its link goes through the project's repo of that name, on GitHub.
  await expect(rows.nth(1).getByTestId("source-link")).toHaveAttribute(
    "href",
    "https://github.com/example-org/agent-skills/commit/0123abc",
  );
  const latest = page.locator("#main").getByTestId("skill-latest");
  await expect(latest.locator(`[data-sha256="${second.sha256}"]`)).toHaveText(second.sha256);
  await expect(latest).toContainText("team-notes-v2.tar.gz");
  await expect(latest).toContainText(`${second.size} byte`);
});

async function downloaded(bundle: Bundle, path: string): Promise<void> {
  const bytes = await readFile(path);
  expect(bytes.length).toBe(bundle.size);
  expect(sha256(bytes)).toBe(bundle.sha256);
}

test("downloading a bundle saves exactly the bytes whose SHA-256 the hub recorded", async ({ page, admin, signInAs }) => {
  const { project, reader, first, second, global, house } = await seeded(admin);
  await signInAs(reader);
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));

  await page.goto(`/p/${project}/skills/team-notes`);
  const latest = page.locator("#main").getByTestId("skill-latest");
  const [latestDownload] = await Promise.all([page.waitForEvent("download"), latest.getByTestId("download-v2").click()]);
  expect(latestDownload.suggestedFilename()).toBe("team-notes-v2.tar.gz");
  await downloaded(second, await latestDownload.path());
  await expect(latest.getByTestId("download-status-v2")).toHaveText("Trình duyệt đang tải team-notes-v2.tar.gz.");
  // The page stays; the bytes never went through a script (no fetch of the bucket from the page).
  await expect(page).toHaveURL(new RegExp(`/p/${project}/skills/team-notes$`));

  const [olderDownload] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#main").getByTestId("skill-versions").getByTestId("download-v1").click(),
  ]);
  expect(olderDownload.suggestedFilename()).toBe("team-notes-v1.tar.gz");
  await downloaded(first, await olderDownload.path());
  const tickets = requests.filter((url) => url.includes("/bundle?"));
  expect(tickets.map((url) => new URL(url).pathname)).toEqual([
    `/v1/skills/projects/${project}/team-notes/bundle`,
    `/v1/skills/projects/${project}/team-notes/bundle`,
  ]);

  await page.goto(`/skills/${global}`);
  const [globalDownload] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#main").getByTestId("skill-latest").getByTestId("download-v1").click(),
  ]);
  await downloaded(house, await globalDownload.path());
});

test("the skills pages fit 375 px without sideways scrolling", async ({ page, admin, signInAs }) => {
  const { project, reader, global } = await seeded(admin);
  await signInAs(reader);
  await page.setViewportSize({ width: 375, height: 812 });
  for (const path of [`/p/${project}/skills`, `/p/${project}/skills/team-notes`, "/skills", `/skills/${global}`]) {
    await page.goto(path);
    await expect(page.locator("#main").getByRole("heading", { level: 1 })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
  }
  // On a phone each version still offers its download.
  await page.goto(`/p/${project}/skills/team-notes`);
  await expect(page.locator("#main").getByTestId("skill-versions").getByTestId("download-v1")).toBeVisible();
});
