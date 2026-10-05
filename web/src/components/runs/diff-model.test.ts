import { describe, expect, it } from "vitest";

import { parseDiff, unquotePath } from "./diff-model";

const DIFF = [
  "diff --git a/src/app.ts b/src/app.ts",
  "index 1111111..2222222 100644",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,3 +1,4 @@ export function main() {",
  " const a = 1;",
  "-const b = 2;",
  "+const b = 3;",
  "+const c = 4;",
  " return a;",
  "diff --git a/docs/new.md b/docs/new.md",
  "new file mode 100644",
  "index 0000000..3333333",
  "--- /dev/null",
  "+++ b/docs/new.md",
  "@@ -0,0 +1 @@",
  "+# New",
  "\\ No newline at end of file",
  "diff --git a/old.txt b/old.txt",
  "deleted file mode 100644",
  "--- a/old.txt",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-gone",
  "diff --git a/a.py b/b.py",
  "similarity index 90%",
  "rename from a.py",
  "rename to b.py",
  "diff --git a/logo.png b/logo.png",
  "index 4444444..5555555 100644",
  "GIT binary patch",
  "literal 12",
  "zcmV+@@-1,1 +1,1 @@",
  "",
  "literal 0",
  "HcmV?d00001",
  "",
  "diff --git a/run.sh b/run.sh",
  "old mode 100644",
  "new mode 100755",
  "",
].join("\n");

describe("parseDiff", () => {
  const parsed = parseDiff(DIFF);

  it("reads every file with its status and counts", () => {
    expect(parsed.files.map((file) => [file.path, file.status, file.additions, file.deletions])).toEqual([
      ["src/app.ts", "modified", 2, 1],
      ["docs/new.md", "added", 1, 0],
      ["old.txt", "deleted", 0, 1],
      ["b.py", "renamed", 0, 0],
      ["logo.png", "modified", 0, 0],
      ["run.sh", "modified", 0, 0],
    ]);
    expect(parsed.files[3].from).toBe("a.py");
    expect(parsed).toMatchObject({ additions: 3, deletions: 2, cut: false });
  });

  it("numbers the lines of each hunk on both sides", () => {
    const [hunk] = parsed.files[0].hunks;
    expect(hunk).toMatchObject({ oldStart: 1, oldLines: 3, newStart: 1, newLines: 4, section: "export function main() {" });
    expect(hunk.lines.map((line) => [line.kind, line.old, line.new, line.text])).toEqual([
      ["context", 1, 1, "const a = 1;"],
      ["removed", 2, null, "const b = 2;"],
      ["added", null, 2, "const b = 3;"],
      ["added", null, 3, "const c = 4;"],
      ["context", 3, 4, "return a;"],
    ]);
    expect(parsed.files[1].hunks[0].lines.at(-1)).toMatchObject({ kind: "note", text: "No newline at end of file" });
  });

  it("notes a binary patch without reading it, and a mode change without text", () => {
    expect(parsed.files[4]).toMatchObject({ binary: true, hunks: [] });
    expect(parsed.files[5]).toMatchObject({ modeOnly: true, binary: false });
  });

  it("stops after the line limit and says so", () => {
    const cut = parseDiff(DIFF, 3);
    expect(cut.cut).toBe(true);
    expect(cut.files[0].hunks[0].lines).toHaveLength(3);
  });

  it("reads paths git quotes", () => {
    expect(unquotePath('"caf\\303\\251 file.txt"')).toBe("café file.txt");
    expect(unquotePath('"tab\\there"')).toBe("tab\there");
    expect(unquotePath("plain.txt")).toBe("plain.txt");
    const quoted = parseDiff(['diff --git "a/caf\\303\\251.md" "b/caf\\303\\251.md"', "--- \"a/caf\\303\\251.md\"", '+++ "b/caf\\303\\251.md"', "@@ -1 +1 @@", "-a", "+b"].join("\n"));
    expect(quoted.files[0].path).toBe("café.md");
    const spaced = parseDiff(["diff --git a/my file.txt b/my file.txt", "old mode 100644", "new mode 100755"].join("\n"));
    expect(spaced.files[0].path).toBe("my file.txt");
  });
});
