type Body = Record<string, unknown>;

export type DiffStat = { added: number; removed: number };

function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function lineCount(text: string): number {
  if (!text) return 0;
  return text.replace(/\n$/, "").split("\n").length;
}

/** The lines an edit adds and removes, from its input (Claude Code, opencode, Codex) or its ACP diff content. */
export function diffStat(input: unknown, content: unknown): DiffStat | null {
  let added = 0;
  let removed = 0;
  let found = false;
  const pair = (before: unknown, after: unknown) => {
    if (typeof before !== "string" && typeof after !== "string") return;
    found = true;
    removed += typeof before === "string" ? lineCount(before) : 0;
    added += typeof after === "string" ? lineCount(after) : 0;
  };
  const unified = (diff: unknown) => {
    if (typeof diff !== "string") return;
    found = true;
    for (const line of diff.split("\n")) {
      if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
      else if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
    }
  };
  if (isRecord(input)) {
    if ("old_string" in input || "new_string" in input) pair(input.old_string, input.new_string);
    else if ("oldString" in input || "newString" in input) pair(input.oldString, input.newString);
    else if (typeof input.content === "string") pair(undefined, input.content);
    if (Array.isArray(input.edits)) for (const edit of input.edits) if (isRecord(edit)) pair(edit.old_string ?? edit.oldString, edit.new_string ?? edit.newString);
    if (Array.isArray(input.changes)) for (const change of input.changes) if (isRecord(change)) unified(change.diff);
  }
  if (!found && Array.isArray(content)) {
    for (const item of content) if (isRecord(item) && item.type === "diff") pair(item.oldText ?? undefined, item.newText);
  }
  return found ? { added, removed } : null;
}


/** An edit's change as lines of a diff (`- old`, `+ new`), for an edit whose output says nothing of it. Mirrors `diffStat`'s sources. */
export function editText(input: unknown, content: unknown): string | null {
  const lines = (text: unknown, mark: string) => (typeof text === "string" && text ? text.replace(/\n$/, "").split("\n").map((line) => `${mark} ${line}`) : []);
  const out: string[] = [];
  const pair = (before: unknown, after: unknown) => {
    if (typeof before !== "string" && typeof after !== "string") return;
    out.push(...lines(before, "-"), ...lines(after, "+"));
  };
  if (isRecord(input)) {
    if ("old_string" in input || "new_string" in input) pair(input.old_string, input.new_string);
    else if ("oldString" in input || "newString" in input) pair(input.oldString, input.newString);
    else if (typeof input.content === "string") pair(undefined, input.content);
    if (Array.isArray(input.edits)) for (const edit of input.edits) if (isRecord(edit)) pair(edit.old_string ?? edit.oldString, edit.new_string ?? edit.newString);
    if (Array.isArray(input.changes)) for (const change of input.changes) if (isRecord(change) && typeof change.diff === "string") out.push(change.diff.replace(/\n$/, ""));
  }
  if (!out.length && Array.isArray(content)) {
    for (const item of content) if (isRecord(item) && item.type === "diff") pair(item.oldText ?? undefined, item.newText);
  }
  return out.length ? out.join("\n") : null;
}
