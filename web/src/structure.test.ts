import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint, type Linter } from "eslint";
import { afterAll, describe, expect, it } from "vitest";

// The size and import-direction rules of eslint/structure.mjs, run through the project's ESLint config, and the checks
// of scripts/check-structure.mjs.
const webDir = fileURLToPath(new URL("..", import.meta.url));
const eslint = new ESLint({ cwd: webDir });

type Baseline = { maxLines: Record<string, number>; imports: Record<string, string[]> };
const baseline = JSON.parse(readFileSync(path.join(webDir, "eslint/structure-baseline.json"), "utf8")) as Baseline;

async function lint(file: string, code: string, rule: string): Promise<Linter.LintMessage[]> {
  const [result] = await eslint.lintText(code, { filePath: path.join(webDir, file) });
  return result.messages.filter((message) => message.ruleId?.startsWith(rule));
}

const lines = (count: number) =>
  Array.from({ length: count }, (_, i) => `export const line${i} = ${i};\n`).join("");

describe("import direction", { timeout: 60_000 }, () => {
  it("keeps a feature folder out of another feature folder, and says how to fix it", async () => {
    const [message, ...rest] = await lint(
      "src/components/skills/probe.ts",
      'export { runsKeys } from "@/components/runs/queries";\n',
      "boundaries/",
    );
    expect(rest).toEqual([]);
    expect(message.message).toContain(
      "This file in components/skills imports src/components/runs/queries.ts from another feature folder.",
    );
    expect(message.message).toContain("Move what both features use down into a shared part or lib");
    expect(message.message).toContain("eslint/structure-baseline.json: that list may only shrink");
  });

  it("lets a feature import its own folder, the shared parts, lib and hooks", async () => {
    const code = [
      'export * from "./queries";',
      'export { Button } from "@/components/ui/button";',
      'export { QueryView } from "@/components/states/query-view";',
      'export { cn } from "@/lib/utils";',
      'export { useIsMobile } from "@/hooks/use-mobile";',
      "",
    ].join("\n");
    expect(await lint("src/components/skills/probe.ts", code, "boundaries/")).toEqual([]);
  });

  it("keeps a shared part and lib below the features", async () => {
    const feature = 'export * from "@/components/runs/queries";\n';
    const [shared] = await lint("src/components/ui/probe.ts", feature, "boundaries/");
    expect(shared.message).toContain("shared part src/components/ui imports src/components/runs/queries.ts");
    expect(shared.message).toContain("take it as a prop or child");

    const [lib] = await lint("src/lib/probe.ts", 'export * from "@/components/ui/button";\n', "boundaries/");
    expect(lib.message).toContain("lib, hooks and i18n are the bottom layer");
  });

  it("lets nothing import a route, and only tests import src/test", async () => {
    const [route] = await lint("src/components/runs/probe.ts", 'export * from "@/app/layout";\n', "boundaries/");
    expect(route.message).toContain("a route file under src/app");

    const helper = 'export { renderWithIntl } from "@/test/render";\n';
    const [notTest] = await lint("src/components/runs/probe.ts", helper, "boundaries/");
    expect(notTest.message).toContain("only *.test.ts or *.test.tsx files import it");
    expect(await lint("src/components/runs/probe.test.ts", helper, "boundaries/")).toEqual([]);
  });

  const [listedFile, listedTargets] = Object.entries(baseline.imports)[0] ?? [];

  it.skipIf(!listedFile)("exempts the imports the baseline lists for a file, and only those", async () => {
    const code = readFileSync(path.join(webDir, listedFile), "utf8");
    expect(await lint(listedFile, code, "boundaries/")).toEqual([]);
    expect(await lint(listedFile, code, "structure/baseline")).toEqual([]);

    const extra = ["secrets", "skills"].find((feature) => !listedFile.includes(`/${feature}/`));
    const more = await lint(listedFile, `${code}export * from "@/components/${extra}/queries";\n`, "boundaries/");
    expect(more).toHaveLength(1);
  });

  it.skipIf(!listedFile)("flags a baseline import the file dropped", async () => {
    const gone = await lint(listedFile, "export {};\n", "structure/baseline");
    for (const target of listedTargets) {
      expect(gone.map((message) => message.message)).toContainEqual(
        `eslint/structure-baseline.json lets this file import ${target}, which it no longer does. Delete that entry ` +
          "so the import cannot come back.",
      );
    }
  });
});

describe("file size", { timeout: 60_000 }, () => {
  it("caps a file at 400 lines and says how to split it", async () => {
    expect(await lint("src/components/runs/probe.ts", lines(400), "structure/max-lines")).toEqual([]);
    const [message] = await lint("src/components/runs/probe.ts", lines(401), "structure/max-lines");
    expect(message.message).toContain("File has 401 lines, over its budget of 400. Split it:");
  });

  it("leaves test files, src/test and the generated API types alone", async () => {
    for (const file of ["src/components/runs/probe.test.ts", "src/test/probe.ts", "src/lib/api/schema.d.ts"]) {
      expect(await lint(file, lines(500), "structure/max-lines"), file).toEqual([]);
    }
  });

  const [longFile, frozen] = Object.entries(baseline.maxLines)[0] ?? [];

  it.skipIf(!longFile)("holds a baselined file at its frozen length and asks to lower it once shorter", async () => {
    const [over] = await lint(longFile, lines(frozen + 1), "structure/max-lines");
    expect(over.message).toContain(`File has ${frozen + 1} lines, over its budget of ${frozen}.`);

    const shorter = await lint(longFile, lines(frozen - 1), "structure/baseline");
    expect(shorter.map((message) => message.message)).toContainEqual(
      `This file has ${frozen - 1} lines, under the ${frozen} frozen for it in eslint/structure-baseline.json. ` +
        `Lower its entry to ${frozen - 1}, or delete the entry once the file has 400 lines or fewer, so it cannot ` +
        "grow back.",
    );
  });
});

describe("scripts/check-structure.mjs", { timeout: 60_000 }, () => {
  const dir = mkdtempSync(path.join(tmpdir(), "check-structure-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  function check(base: Baseline) {
    const file = path.join(dir, "base.json");
    writeFileSync(file, JSON.stringify(base));
    const args = ["scripts/check-structure.mjs", "--base", file];
    return spawnSync(process.execPath, args, { cwd: webDir, encoding: "utf8" });
  }

  it("passes a baseline equal to main's, with no import cycle in src", () => {
    const result = check(baseline);
    expect(result.stderr).toBe("");
    expect(result.stdout).toContain("check-structure: no import cycle among");
    expect(result.stdout).toContain("nothing added or raised against");
    expect(result.status).toBe(0);
  });

  const [listedFile, [listedTarget, ...otherTargets] = []] = Object.entries(baseline.imports)[0] ?? [];
  const [longFile, frozen] = Object.entries(baseline.maxLines)[0] ?? [];

  it.skipIf(!listedFile || !longFile)("fails a baseline that grew against main's", () => {
    const result = check({
      maxLines: { ...baseline.maxLines, [longFile]: frozen - 1 },
      imports: { ...baseline.imports, [listedFile]: otherTargets },
    });
    expect(result.stderr).toContain(
      `eslint/structure-baseline.json: maxLines: ${longFile} is frozen at ${frozen - 1} on main, not ${frozen}.`,
    );
    expect(result.stderr).toContain(
      `eslint/structure-baseline.json: imports: ${listedFile} -> ${listedTarget} is not in the baseline on main.`,
    );
    expect(result.status).toBe(1);
  });
});
