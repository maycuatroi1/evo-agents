import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "./support/fixtures";
import { signOut } from "./support/auth";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { extractUploads, grantOn, kgProject, open, uploadPath } from "./support/kg";

/**
 * Upload data and New project from data (plan kg-upload-archive, a7) against the local stack, with a small fictional
 * archive (e2e/fixtures/lumen-notes.zip: a markdown guide, a CSV, a PNG kept without text, and an entry whose path
 * leaves the archive). The parts go through the hub's relay to the stack's fake S3; the worker's read of the archive
 * and the build it queues run through the stack (POST /kg/extract). With E2E_SCREENSHOT_DIR set, it also takes a
 * screenshot of each page.
 */
const ARCHIVE = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures", "lumen-notes.zip");
const shots = process.env.E2E_SCREENSHOT_DIR;

test.describe("upload data", () => {
  test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "reads uploads through the local stack");
  test.setTimeout(90_000);

  test("an upload stopped by a failing part resumes after a reload, then reports what the hub read", async ({ page, admin, signInAs }) => {
    const { project, writer } = await kgProject(admin, "kg-upload");
    await signInAs(writer);
    await open(page, uploadPath(project));

    await expect(page.getByTestId("kg-tab-upload")).toHaveAttribute("aria-current", "page");
    await expect(page.getByTestId("kg-read-only")).toHaveCount(0);
    await expect(page.getByTestId("kg-uploads-empty")).toBeVisible();
    // The labels stop at what the project's hub sink clears (customer): secret is not offered.
    const label = page.getByTestId("kg-upload-label");
    await expect(label.locator("option")).toHaveText(["public", "internal", "customer"]);
    await expect(label).toHaveValue("internal");

    // A file the hub does not read is refused before anything is sent.
    await page.getByTestId("kg-upload-input").setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("x") });
    await expect(page.getByTestId("kg-upload-refused")).toHaveAttribute("data-kind", "notArchive");

    // Every try of part 1 fails: the upload stops and says so.
    const part = `**/v1/kg/${project}/uploads/*/parts/1`;
    await page.route(part, (route) => route.abort("connectionreset"));
    await page.getByTestId("kg-upload-input").setInputFiles(ARCHIVE);
    await expect(page.getByTestId("kg-upload-refused")).toHaveCount(0);
    await expect(page.getByTestId("kg-upload-file")).toContainText("lumen-notes.zip");
    await expect(page.getByTestId("kg-upload-source")).toHaveAttribute("placeholder", "lumen-notes");
    await label.selectOption("customer");
    await page.getByTestId("kg-upload-start").click();
    await expect(page.getByTestId("kg-upload-failed")).toBeVisible({ timeout: 20_000 });
    await page.unroute(part);

    // After a reload, the page names the unfinished upload; the same file resumes it, with the label it started with.
    await open(page, uploadPath(project));
    await expect(page.getByTestId("kg-upload-pending-item")).toContainText("lumen-notes.zip");
    await expect(page.getByTestId("kg-upload-report")).toHaveAttribute("data-state", "uploading");
    await page.getByTestId("kg-upload-input").setInputFiles(ARCHIVE);
    await expect(page.getByTestId("kg-upload-start")).toHaveText("Resume upload");
    await expect(page.getByTestId("kg-upload-label")).toHaveValue("customer");
    await page.getByTestId("kg-upload-start").click();
    await expect(page.getByTestId("kg-upload-progress")).toHaveAttribute("data-phase", "done");
    await expect(page.getByTestId("kg-upload-pending")).toHaveCount(0);
    const report = page.getByTestId("kg-upload-report");
    await expect(report).toHaveCount(1);
    await expect(report).toHaveAttribute("data-state", "extracting");
    await expect(report.getByTestId("kg-upload-moving")).toHaveText("Reading");

    // The worker reads it and builds: the report says what was read and skipped, by reason, and links the build.
    const { uploads } = await extractUploads(project, writer);
    expect(uploads[0]?.state, uploads[0]?.error ?? "").toBe("extracted");
    await open(page, uploadPath(project));
    await expect(report).toHaveAttribute("data-state", "extracted");
    await expect(report.getByTestId("kg-upload-files")).toHaveText("3");
    await expect(report.getByTestId("kg-upload-skipped")).toHaveAttribute("data-count", "1");
    await expect(report.getByTestId("kg-upload-skipped")).toContainText("Unsafe path: 1");
    await expect(report.getByTestId("kg-upload-assets")).toContainText("Binary file: 1");
    await expect(report.getByTestId("kg-upload-state")).toHaveCount(0); // nothing went wrong: no pill
    // This project has no knowledge config on the hub yet (no sync pushed one), so no build follows the read.
    await expect(report.getByTestId("kg-upload-build-link")).toHaveCount(0);
    await expect(report).toContainText("Label customer");
    await report.locator("summary").click();
    await expect(report.getByTestId("kg-upload-paths")).toContainText("../outside.txt");
    if (shots) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(shots, "kg-upload-page.png"), fullPage: true });
    }
  });

  test("a reader is told why the page takes no upload", async ({ page, admin, signInAs }) => {
    const { project } = await kgProject(admin, "kg-upload-read");
    const reader = newAccount("kg-upload-reader");
    await grantOn(project, reader.login, "reader", "internal");
    await signInAs(reader);
    await open(page, uploadPath(project));
    await expect(page.getByTestId("kg-upload-not-writer")).toBeVisible();
    await expect(page.getByTestId("kg-upload-input")).toHaveCount(0);
  });

  test("a hub admin makes a project from data; a member without the right is told why", async ({ page, signInAs }) => {
    await signInAs(ADMIN_ACCOUNT);
    await open(page, "/");
    await page.getByTestId("home-new-project").first().click();
    await expect(page).toHaveURL(/\/projects\/new$/);
    await expect(page.getByRole("heading", { level: 1, name: "New project from data" })).toBeVisible();

    const name = page.getByTestId("kg-new-project-name");
    await name.fill("Not A Name");
    await name.blur();
    await expect(page.getByTestId("kg-new-project-name-error")).toBeVisible();
    const project = uniqueName("lumen");
    await name.fill(project);
    await expect(page.getByTestId("kg-new-project-name-error")).toHaveCount(0);
    await expect(page.getByTestId("kg-upload-label").locator("option")).toHaveText(["public", "internal", "customer", "secret"]);
    await expect(page.getByTestId("kg-upload-label")).toHaveValue("internal");
    await page.getByTestId("kg-upload-input").setInputFiles(ARCHIVE);
    if (shots) await page.screenshot({ path: path.join(shots, "kg-new-project-page.png"), fullPage: true });
    await page.getByTestId("kg-new-project-create").click();
    await page.getByTestId("kg-new-project-open").click();
    await expect(page).toHaveURL(new RegExp(`/p/${project}/kg/upload$`));
    await expect(page.getByTestId("kg-upload-report")).toHaveAttribute("data-state", "extracting");
    const { uploads } = await extractUploads(project, ADMIN_ACCOUNT);
    expect(uploads[0]?.state, uploads[0]?.error ?? "").toBe("extracted");
    await open(page, uploadPath(project));
    await expect(page.getByTestId("kg-upload-files")).toHaveText("3");
    // The new project has the hub's minimal knowledge config: its first build followed the read, and the report links it.
    const built = uploads[0]?.build_id;
    expect(built, "the build queued after the read").not.toBeNull();
    await expect(page.getByTestId("kg-upload-build-link")).toHaveText(`Build #${built}`);
    await expect(page.getByTestId("kg-upload-build-link")).toHaveAttribute("href", `/p/${project}/kg/builds`);
    await expect(page.getByTestId("kg-upload-build-state")).toHaveCount(0);

    // A member with no register right is refused.
    await signOut(page);
    await signInAs(newAccount("kg-no-right"));
    await open(page, "/projects/new");
    await page.getByTestId("kg-new-project-name").fill(uniqueName("lumen"));
    await page.getByTestId("kg-upload-input").setInputFiles(ARCHIVE);
    await page.getByTestId("kg-new-project-create").click();
    await expect(page.getByTestId("kg-new-project-forbidden")).toBeVisible();
  });
});
