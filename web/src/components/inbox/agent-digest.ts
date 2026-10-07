import { type Tone, type ToolKind, toolLabel, type ToolStatus, type TraceItem } from "@/components/runs/trace-model";

/**
 * "What the agent did so far" on the phone's decision screen: the last few things the run's Trace shows, one line each,
 * read from the trace model (`buildTrace`) of the run's latest events. Moves between states and runtime events the
 * trace shows raw are left out, as is the decision on screen itself: what is left is what the agent said, ran, planned
 * and was told. Pure functions, shared by the screen and its tests.
 */

/** Lines in the summary. */
export const DIGEST_ITEMS = 5;
/** Characters of a message or a line of text kept on its line. */
export const DIGEST_CHARS = 160;

type Line = { key: string; at: string };
export type DigestLine =
  | (Line & { type: "agent"; text: string; thoughtMs: number | null })
  | (Line & { type: "tool"; kind: ToolKind; name: string | null; arg: string | null; status: ToolStatus; failed: boolean; exitCode: number | null; ms: number | null })
  | (Line & { type: "plan"; done: number; total: number })
  | (Line & { type: "user"; from: string | null; text: string; decisionId: number | null })
  | (Line & { type: "system"; text: string; tone: Tone })
  | (Line & { type: "ask"; question: string; decisionId: number });

/**
 * Markdown read as plain words: bold and italic asterisks, code marks, strike-through, headings, quotes and list marks
 * dropped, a link as its text. Underscores stay, since a name like `__init__.py` holds them.
 */
export function plainText(markdown: string): string {
  return markdown
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d+[.)][ \t]+)/gm, "")
    .replace(/(\*\*|`+|~~)/g, "")
    .replace(/(^|[\s(])\*(\S(?:.*?\S)?)\*(?=[\s).,;:!?]|$)/g, "$1$2");
}

/** The words of `text` on one line: whitespace and line breaks as single spaces, cut at `max` characters with "…". */
export function oneLine(text: string, max = DIGEST_CHARS): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, max - 1).trimEnd()}…`;
}

function line(item: TraceItem): DigestLine | null {
  const base = { key: item.key, at: item.at };
  switch (item.type) {
    case "agent": {
      const text = oneLine(plainText(item.text));
      if (!text && !item.thought) return null;
      return { ...base, type: "agent", text, thoughtMs: text ? null : (item.thought?.ms ?? null) };
    }
    case "tool": {
      const { tool } = item;
      const { name, arg } = toolLabel(tool);
      return {
        ...base,
        type: "tool",
        kind: tool.kind,
        name,
        arg: arg === null ? null : oneLine(arg),
        status: tool.status,
        failed: tool.status === "failed" || (tool.exitCode !== null && tool.exitCode !== 0),
        exitCode: tool.exitCode,
        ms: tool.ms,
      };
    }
    case "plan":
      return { ...base, type: "plan", done: item.entries.filter((entry) => entry.status === "completed").length, total: item.entries.length };
    case "user":
      return { ...base, type: "user", from: item.from, text: oneLine(item.text), decisionId: item.decisionId };
    case "system":
      return { ...base, type: "system", text: oneLine(item.text), tone: item.tone };
    case "ask":
      return { ...base, type: "ask", question: oneLine(item.question), decisionId: item.decisionId };
    case "move":
    case "raw":
      return null;
  }
}

/**
 * The last `count` things of the trace, oldest first, one line each; `decisionId` is the decision on screen, whose
 * "Asked you" line would repeat the question above.
 */
export function agentDigest(items: readonly TraceItem[], decisionId: number, count = DIGEST_ITEMS): DigestLine[] {
  const lines: DigestLine[] = [];
  for (let index = items.length - 1; index >= 0 && lines.length < count; index -= 1) {
    const item = items[index];
    if (item.type === "ask" && item.decisionId === decisionId) continue;
    const found = line(item);
    if (found) lines.push(found);
  }
  return lines.reverse();
}
