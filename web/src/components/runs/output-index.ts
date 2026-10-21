import { baseName, outputLinks, type RunOutput } from "@/lib/outputs";

/**
 * Where a run's outputs show in its Trace: each under the tool call it came from. The hub ties an output to a call
 * three ways, read here in this order, each output going to one call only:
 *
 * - its `tool_call_id`: an image a tool's result held, a file SendUserFile sent, a page the Artifact tool published;
 * - a link in the call's `tool_call_update` (the ACP `resource_link` `evo-output:sha256:<hex>` that stands in an event
 *   for an image taken out of it), matched by SHA-256, never by name (a local session names its images otherwise);
 * - a shell call of `evo-agents worker output FILE...` or `evo-agents hub send FILE...`, which names no call: a file
 *   it sent, of source sent and tied to no call, whose name is a FILE's and which came after the call started.
 *
 * What no call holds (the files of `.evo-run/outputs/`, a file sent with no call to match) shows under the hub's
 * `system` event that recorded it (`{"text", "outputs": [ids]}`); a system event whose outputs all show under calls is
 * left out of the Trace, as the calls say it already. Pure functions, shared by the page and its tests.
 */

export type OutputIndex = {
  /** Every output, oldest first. */
  all: readonly RunOutput[];
  byId: ReadonlyMap<number, RunOutput>;
  /** The outputs shown under each tool call, by its id. */
  byCall: ReadonlyMap<string, RunOutput[]>;
  /** The call an output shows under. */
  callOf: ReadonlyMap<number, string>;
  /** The outputs a system event recorded that no call shows, by the event's seq. */
  bySystem: ReadonlyMap<number, RunOutput[]>;
  /** System events of recorded outputs that the calls show already: the Trace leaves them out. */
  hiddenSystem: ReadonlySet<number>;
  /** The seq of the system event that recorded an output no call shows. */
  systemOf: ReadonlyMap<number, number>;
};

const SEND_COMMAND = /\bevo-agents\s+(?:worker\s+output|hub\s+send)\s+(.+)$/s;
/** Options of those commands that take a value, whose value is no file. */
const VALUED = new Set(["--caption", "--session", "--wait", "--project"]);
/** A send command's call may start a moment before the hub's clock says the file came: the two clocks differ. */
const SKEW_MS = 5_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function callId(body: Record<string, unknown>): string | null {
  const id = body.toolCallId ?? body.id;
  return typeof id === "string" && id.trim() !== "" ? id : null;
}

/** The words of a shell line, quotes taken off: enough to read the files a send command names. */
function words(text: string): string[] {
  const found: string[] = [];
  for (const match of text.matchAll(/"((?:\\.|[^"\\])*)"|'([^']*)'|(\S+)/g)) found.push(match[1] ?? match[2] ?? match[3]);
  return found;
}

/** The base names of the files `evo-agents worker output` or `evo-agents hub send` names in `command`; none otherwise. */
export function sentFiles(command: string): string[] {
  const match = SEND_COMMAND.exec(command.split(/&&|\|\||;|\n/).find((part) => SEND_COMMAND.test(part)) ?? "");
  if (!match) return [];
  const files: string[] = [];
  const parts = words(match[1]);
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (part.startsWith("-")) {
      if (VALUED.has(part)) index += 1;
      continue;
    }
    if (/^[|&;<>]/.test(part)) break;
    files.push(baseName(part));
  }
  return files;
}

function commandOf(body: Record<string, unknown>): string | null {
  const input = body.rawInput ?? body.input ?? body.arguments;
  if (isRecord(input)) {
    const command = input.command ?? input.cmd;
    if (typeof command === "string") return command;
    if (Array.isArray(command) && command.every((part) => typeof part === "string")) return command.join(" ");
  }
  return typeof body.title === "string" ? body.title : null;
}

/** An event of the run's log, as the index reads it. */
export type IndexEvent = { seq: number; at: string; kind: string; body: unknown };

export function buildOutputIndex(outputs: readonly RunOutput[], events: readonly IndexEvent[]): OutputIndex {
  const byId = new Map(outputs.map((output) => [output.id, output]));
  const bySha = new Map(outputs.map((output) => [output.sha256, output]));
  const callOf = new Map<number, string>();
  const calls = new Set<string>();
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  const sends: { id: string; at: number; files: string[] }[] = [];

  for (const event of ordered) {
    const body = isRecord(event.body) ? event.body : {};
    if (event.kind !== "tool_call" && event.kind !== "tool_call_update") continue;
    const id = callId(body);
    if (id === null) continue;
    calls.add(id);
    if (event.kind === "tool_call") {
      const command = commandOf(body);
      const files = command ? sentFiles(command) : [];
      if (files.length) sends.push({ id, at: Date.parse(event.at), files });
    }
    for (const link of outputLinks(body.content)) {
      const output = bySha.get(link.sha256);
      if (output && !callOf.has(output.id)) callOf.set(output.id, id);
    }
  }
  // A call's own id wins over a link: it is what the machine recorded the output with.
  for (const output of outputs) if (output.tool_call_id !== null && calls.has(output.tool_call_id)) callOf.set(output.id, output.tool_call_id);
  for (const send of sends) {
    for (const file of send.files) {
      const output = outputs.find(
        (candidate) =>
          candidate.source === "sent" &&
          !callOf.has(candidate.id) &&
          (candidate.tool_call_id === null || !calls.has(candidate.tool_call_id)) &&
          baseName(candidate.name) === file &&
          Date.parse(candidate.created_at) >= send.at - SKEW_MS,
      );
      if (output) callOf.set(output.id, send.id);
    }
  }

  const byCall = new Map<string, RunOutput[]>();
  for (const output of outputs) {
    const id = callOf.get(output.id);
    if (id !== undefined) byCall.set(id, [...(byCall.get(id) ?? []), output]);
  }
  const bySystem = new Map<number, RunOutput[]>();
  const hiddenSystem = new Set<number>();
  const systemOf = new Map<number, number>();
  for (const event of ordered) {
    const body = isRecord(event.body) ? event.body : {};
    if (event.kind !== "system" || !Array.isArray(body.outputs)) continue;
    const recorded = body.outputs.filter((id): id is number => typeof id === "number");
    const left = recorded.flatMap((id) => {
      const output = byId.get(id);
      return output && !callOf.has(id) && !systemOf.has(id) ? [output] : [];
    });
    if (left.length) {
      bySystem.set(event.seq, left);
      for (const output of left) systemOf.set(output.id, event.seq);
    } else if (recorded.length && recorded.every((id) => callOf.has(id) || systemOf.has(id) || !byId.has(id))) {
      hiddenSystem.add(event.seq);
    }
  }
  return { all: outputs, byId, byCall, callOf, bySystem, hiddenSystem, systemOf };
}

/** The tools whose whole point is to send files: their row shows the files instead of the paths they were given. */
export function sendsFiles(tool: { title: string | null; arg: string | null }): boolean {
  if (tool.title === "SendUserFile" || tool.title === "Artifact") return true;
  return tool.arg !== null && sentFiles(tool.arg).length > 0;
}
