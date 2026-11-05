import { type RunMove } from "./log-model";
import type { RunEvent } from "./queries";
import { type DiffStat } from "./trace-diff";

/** Lines of a tool's output shown before "Show the full output". */
export const OUTPUT_LINES = 20;
/** Above this many items the trace renders only the ones in view. */
export const TRACE_VIRTUAL_THRESHOLD = 500;
export const TOOL_KINDS = ["read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"] as const;
export type ToolKind = (typeof TOOL_KINDS)[number];
export type ToolStatus = "pending" | "in_progress" | "completed" | "failed";
export type Tone = "ok" | "error" | null;

export type ToolCall = {
  /** The runtime's toolCallId; null when the event had none. */
  id: string | null;
  /** The runtime's title for it ("Bash", "bash", the command itself for Codex), null until the call is known. */
  title: string | null;
  kind: ToolKind;
  /** What it was asked to do, in one line: its command, path, pattern or query. */
  arg: string | null;
  status: ToolStatus;
  startedAt: string;
  /** When its last update with a final status came. */
  endedAt: string | null;
  /** From the times of its events; null while it runs. */
  ms: number | null;
  exitCode: number | null;
  /** What the agent said the call is for, when it said (Claude Code's `description`). */
  description: string | null;
  /** What it printed or returned, whole; the page cuts it at OUTPUT_LINES. */
  output: string | null;
  /** An edit's lines added and removed, when its input or output says. */
  diff: DiffStat | null;
  /** An edit's change as diff lines (`- old`, `+ new`), from its input. */
  change: string | null;
  /** The input whole, as JSON, for a read, search or other call whose input says more than `arg`. */
  input: string | null;
};

export type Thought = { text: string; ms: number };
export type PlanEntry = { content: string; status: "pending" | "in_progress" | "completed"; priority: string | null };
export type RawEvent = { seq: number; at: string; label: string; json: string };

export type Base = { key: string; seq: number; at: string };
export type AgentItem = Base & { type: "agent"; text: string; thought: Thought | null; endAt: string };
export type ToolItem = Base & { type: "tool"; tool: ToolCall };
export type TraceItem =
  | AgentItem
  | ToolItem
  | (Base & { type: "plan"; entries: PlanEntry[] })
  | (Base & { type: "user"; text: string; from: string | null; decisionId: number | null; files: unknown })
  | (Base & { type: "move"; move: RunMove })
  | (Base & { type: "system"; text: string; tone: Tone; output: string | null })
  | (Base & { type: "ask"; decisionId: number; question: string; category: string | null; step: string | null })
  | (Base & { type: "raw"; events: RawEvent[] });

export type TraceEvent = Pick<RunEvent, "seq" | "at" | "kind" | "body">;
