import { moveOf } from "./log-model";
import { type DiffStat, diffStat } from "./trace-diff";
import { isRecord, str, int, json, between, messageText, newTool, applyCall, applyUpdate, planEntries, askedQuestion, rawEvent, SHOWN_KINDS, latestOpen, type Body } from "./trace-events";
import { OUTPUT_LINES, type ToolCall, type RawEvent, type Base, type AgentItem, type TraceItem, type TraceEvent } from "./trace-types";

export { type DiffStat, diffStat };
export { OUTPUT_LINES, TOOL_KINDS, TRACE_VIRTUAL_THRESHOLD } from "./trace-types";
export type { AgentItem, PlanEntry, RawEvent, Thought, Tone, ToolCall, ToolItem, ToolKind, ToolStatus, TraceEvent, TraceItem } from "./trace-types";

/**
 * How the Trace tab of a run's page reads the run's events (the same stream as the Raw log): one item per thing the
 * agent did, in the order it did them.
 *
 * - The agent's words: consecutive `agent_message_chunk` events are one message. A turn ends at anything else the
 *   trace shows, and at a `usage_update` (a runtime reports usage once a turn or a step ends). The adapters of this
 *   repository send whole blocks of text, so chunks are joined as paragraphs.
 * - Its thinking: `agent_thought_chunk` events fold into the message that follows them ("Thought for 9s"), timed from
 *   the event before the first of them to the last of them, or stand alone when a tool call follows.
 * - Tool calls: a `tool_call` and its `tool_call_update` events, matched by `toolCallId`, are one row with its title,
 *   kind, main argument, status, duration (from the times of the events), exit code when the runtime gives one, and
 *   output (the update's `content`, else its `rawOutput`). An update that comes before its call makes the row, and the
 *   call fills it in when it comes.
 * - A `plan` is a checklist, a `user_message` the owner's words (or answer to a decision), a `state` a move between
 *   states, a `system` event a line of the worker's, or "Asked you" when it reports a decision the agent asked.
 * - `output` events, a runtime's events no adapter maps, fold together in their raw form; so does an event of a kind
 *   this page does not know.
 *
 * Events are read in seq order whatever order they come in, and each seq once. Bodies come from three runtimes, so
 * every key is read defensively. Pure functions, shared by the page and its tests.
 */

/** A tool's own name, as opposed to a command or a path some runtimes give as its title. */
const TOOL_NAME = /^[A-Za-z][A-Za-z0-9_:-]{0,47}$/;

/**
 * How a tool call is named: a tool's own name ("Bash", "TodoWrite", "mcp__hub__search") heads it, null when the runtime
 * gave none (the caller names the kind instead); a title that is a command or a path (Codex, opencode) is what the call
 * did, as its argument when the input names nothing.
 */
export function toolLabel(tool: Pick<ToolCall, "title" | "arg">): { name: string | null; arg: string | null } {
  const name = tool.title !== null && TOOL_NAME.test(tool.title) ? tool.title : null;
  return { name, arg: tool.arg ?? (name ? null : tool.title) };
}

/**
 * The run's events as the Trace shows them. `events` may come in any order and repeat a seq; the trace reads each seq
 * once, in order.
 */
export function buildTrace(events: readonly TraceEvent[]): TraceItem[] {
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  const items: TraceItem[] = [];
  const tools = new Map<string, ToolCall>();
  let agent: AgentItem | null = null;
  let raw: (Base & { type: "raw"; events: RawEvent[] }) | null = null;
  let previousAt: string | null = null;
  let lastSeq = -Infinity;

  const close = () => {
    agent = null;
    raw = null;
  };

  for (const event of ordered) {
    if (event.seq === lastSeq) continue;
    lastSeq = event.seq;
    const body: Body = isRecord(event.body) ? event.body : {};
    const kind = SHOWN_KINDS.has(event.kind) ? event.kind : "unknown";
    const base = { seq: event.seq, at: event.at };

    switch (kind) {
      case "agent_thought_chunk": {
        raw = null;
        const text = messageText(body) ?? "";
        const start = previousAt ?? event.at;
        const open = agent as AgentItem | null;
        if (open && open.text === "" && open.thought) {
          open.thought.text = [open.thought.text, text].filter(Boolean).join("\n\n");
          open.thought.ms += between(open.endAt, event.at);
          open.endAt = event.at;
        } else {
          const item: AgentItem = { ...base, key: `a${event.seq}`, type: "agent", text: "", thought: { text, ms: between(start, event.at) }, endAt: event.at };
          items.push(item);
          agent = item;
        }
        break;
      }
      case "agent_message_chunk": {
        raw = null;
        const text = messageText(body) ?? "";
        const open = agent as AgentItem | null;
        if (open) {
          open.text = open.text && text ? `${open.text}\n\n${text}` : open.text || text;
          open.endAt = event.at;
        } else {
          const item: AgentItem = { ...base, key: `a${event.seq}`, type: "agent", text, thought: null, endAt: event.at };
          items.push(item);
          agent = item;
        }
        break;
      }
      case "tool_call": {
        close();
        const id = str(body.toolCallId) ?? str(body.id);
        const known = id !== null ? tools.get(id) : undefined;
        if (known) {
          applyCall(known, body, event.at, false);
        } else {
          const tool = newTool(event, id);
          applyCall(tool, body, event.at, true);
          if (id !== null) tools.set(id, tool);
          items.push({ ...base, key: `t${event.seq}`, type: "tool", tool });
        }
        break;
      }
      case "tool_call_update": {
        // An update changes a row already shown (or makes one): the agent's message around it goes on. One without an
        // id, which ACP does not allow but a hand-made event may be, goes to the latest call still without a result.
        const id = str(body.toolCallId) ?? str(body.id);
        let tool = id !== null ? tools.get(id) : latestOpen(items);
        if (!tool) {
          raw = null;
          tool = newTool(event, id);
          if (id !== null) tools.set(id, tool);
          items.push({ ...base, key: `t${event.seq}`, type: "tool", tool });
        }
        applyUpdate(tool, body, event.at);
        break;
      }
      case "plan":
        close();
        items.push({ ...base, key: `p${event.seq}`, type: "plan", entries: planEntries(body) });
        break;
      case "user_message":
        close();
        items.push({
          ...base,
          key: `u${event.seq}`,
          type: "user",
          text: messageText(body) ?? (Array.isArray(body.attachments) ? "" : json(body)),
          from: str(body.from),
          decisionId: int(body.decision_id), files: body.attachments,
        });
        break;
      case "state": {
        close();
        const move = moveOf(event);
        if (move) items.push({ ...base, key: `s${event.seq}`, type: "move", move });
        else items.push({ ...base, key: `s${event.seq}`, type: "system", text: json(body), tone: null, output: null });
        break;
      }
      case "system": {
        close();
        const text = messageText(body) ?? json(body);
        const decision = isRecord(body.decision) ? body.decision : null;
        const decisionId = decision ? int(decision.id) : null;
        if (decision && decisionId !== null) {
          items.push({
            ...base,
            key: `q${event.seq}`,
            type: "ask",
            decisionId,
            question: askedQuestion(text),
            category: str(decision.category),
            step: str(decision.step) ?? (int(decision.step) !== null ? String(decision.step) : null),
          });
        } else {
          const code = int(body.exit_code);
          const output = str(body.output);
          items.push({
            ...base,
            key: `y${event.seq}`,
            type: "system",
            text,
            tone: code === null ? null : code === 0 ? "ok" : "error",
            output: output ? output.replace(/\s+$/, "") : null,
          });
        }
        break;
      }
      case "usage_update":
        // A turn (or a step) ended; its figures go to the usage card.
        close();
        break;
      default: {
        agent = null;
        const open = raw as (Base & { type: "raw"; events: RawEvent[] }) | null;
        if (open) open.events.push(rawEvent(event, kind === "unknown" ? event.kind : kind));
        else {
          const item = { ...base, key: `r${event.seq}`, type: "raw" as const, events: [rawEvent(event, kind === "unknown" ? event.kind : kind)] };
          items.push(item);
          raw = item;
        }
      }
    }
    previousAt = event.at;
  }
  return items;
}

/** The first OUTPUT_LINES lines of `text`, and how many lines it has in all. */
export function cutOutput(text: string, lines = OUTPUT_LINES): { shown: string; total: number; cut: boolean } {
  const all = text.replace(/\n$/, "").split("\n");
  return { shown: all.slice(0, lines).join("\n"), total: all.length, cut: all.length > lines };
}

/** Whether a tool row starts open: a failed call is open, so what went wrong shows without a click. */
export function openByDefault(tool: ToolCall): boolean {
  return tool.status === "failed" || (tool.exitCode !== null && tool.exitCode !== 0);
}

/** A duration as its parts, for "48.2s", "8m 12s", "1h 5m". */
export function durationParts(ms: number): { hours: number; minutes: number; seconds: number; tenths: number } {
  const tenths = Math.floor(ms / 100);
  const seconds = Math.floor(ms / 1000);
  return { hours: Math.floor(seconds / 3600), minutes: Math.floor(seconds / 60) % 60, seconds: seconds % 60, tenths: tenths % 10 };
}
