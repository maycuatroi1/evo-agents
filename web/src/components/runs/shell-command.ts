/**
 * The shell commands of tool calls: the line a runtime's shell ran, and what it does once its setup is done, which a
 * tool row folds to.
 */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `/bin/zsh -lc 'ls -a'` as `ls -a`: Codex names a command by the shell line that ran it. */
export function unwrapShell(command: string): string {
  const match = /^\s*(?:\/(?:usr\/)?bin\/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1\s*$/.exec(command);
  return match ? match[2] : command;
}

/**
 * One step of setup at the head of a shell line, with the `&&` or `;` after it: `cd` to a directory, `export`, `source`
 * or `.` of a file, `set -e`, or variables assigned for what follows.
 */
const SETUP =
  /^\s*(?:cd\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)|export\s+[^&;|]+?|(?:source|\.)\s+[^\s;&|]+|set\s+[-+][A-Za-z]+|[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*)(?:\s+[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*))*)\s*(?:&&|;)\s*/;

/**
 * What a shell line does once its setup is done: `cd ~/wt && export PATH=... && pytest -q` as `pytest -q`. Agents
 * start most commands in the worktree that way, which on a narrow row leaves only the `cd` in view. A line that is all
 * setup stays whole.
 */
export function commandGist(command: string): string {
  let rest = command;
  for (let match = SETUP.exec(rest); match && match[0].length < rest.length; match = SETUP.exec(rest)) {
    rest = rest.slice(match[0].length);
  }
  return rest === command ? command : rest.trim() || command;
}

/** A command shorter than this, that is its own gist on one line, says nothing more as an Input. */
const SHORT_COMMAND = 48;

/** The whole command a shell call ran, for its Input once the row is open; null when the folded line already says it. */
export function commandInput(input: unknown): string | null {
  if (!isRecord(input)) return null;
  const value = input.command ?? input.cmd;
  const raw = typeof value === "string" ? value : Array.isArray(value) && value.every((item) => typeof item === "string") ? value.join(" ") : null;
  const command = raw ? unwrapShell(raw).trim() : "";
  if (!command) return null;
  return command.includes("\n") || command.length >= SHORT_COMMAND || commandGist(command) !== command ? command : null;
}

/** A path inside a worker's worktree as the repo knows it: `~/.evo/worker/worktrees/x-80/api/src/a.ts` as `src/a.ts`. */
export function worktreePath(path: string): string {
  const match = /\/worktrees\/[^/]+\/[^/]+\/(.+)$/.exec(path);
  return match ? match[1] : path;
}
