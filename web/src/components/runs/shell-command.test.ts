import { describe, expect, it } from "vitest";

import { commandGist, commandInput, unwrapShell } from "./shell-command";

describe("shell commands", () => {
  it("unwraps the shell line Codex names a command by", () => {
    expect(unwrapShell("/bin/zsh -lc 'ls -a'")).toBe("ls -a");
    expect(unwrapShell(`bash -c "echo 'x'"`)).toBe("echo 'x'");
    expect(unwrapShell("ls -a")).toBe("ls -a");
  });

  it("folds a command to what it does once its setup is done", () => {
    const worktree = "cd /Users/me/.evo/worker/worktrees/evo-agents-80/evo-agents";
    expect(commandGist(`${worktree} && ruff check . 2>&1 | tail -3`)).toBe("ruff check . 2>&1 | tail -3");
    expect(commandGist(`${worktree} && export PATH=/tmp/venv/bin:$PATH EVO_HUB_TEST_DSN=postgresql://u:p@localhost:5432/db && pytest -q`)).toBe("pytest -q");
    expect(commandGist(`cd "/a b" ; source .venv/bin/activate && set -e && FOO=1 BAR='x y' && make test`)).toBe("make test");
    expect(commandGist(". ./env.sh && go test ./...")).toBe("go test ./...");
    // An assignment for the command itself, and a pipe or a second command after the work, stay.
    expect(commandGist("FOO=1 make && cd docs")).toBe("FOO=1 make && cd docs");
    expect(commandGist("cd web && pnpm test || true")).toBe("pnpm test || true");
    // A line that is all setup stays whole.
    expect(commandGist("cd /tmp")).toBe("cd /tmp");
    expect(commandGist("cd /tmp && ")).toBe("cd /tmp && ");
  });

  it("keeps the whole command as a call's Input when the folded line does not say it all", () => {
    expect(commandInput({ command: "pytest -q" })).toBeNull();
    expect(commandInput({ command: "cd /wt && pytest -q" })).toBe("cd /wt && pytest -q");
    expect(commandInput({ command: "python3 - <<'E'\nprint(1)\nE" })).toBe("python3 - <<'E'\nprint(1)\nE");
    expect(commandInput({ command: "pytest -q tests/hub/test_queue.py tests/hub/test_runs.py" })).toBe("pytest -q tests/hub/test_queue.py tests/hub/test_runs.py");
    expect(commandInput({ cmd: ["/bin/zsh", "-lc", "'cd /wt && ls'"] })).toBe("cd /wt && ls");
    expect(commandInput({ file_path: "a.ts" })).toBeNull();
    expect(commandInput("ls")).toBeNull();
  });
});
