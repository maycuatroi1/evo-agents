import type { RunEvent } from "./queries";

/**
 * A run's tokens and cost as the usage card shows them, from what the runtime reported: the run's `usage` once the
 * worker reported its end, else the `usage_update` events while it runs. Nothing is estimated: a cost the runtime did
 * not report is null, never a price times tokens.
 *
 * The three runtimes report differently (checked with real runs of their adapters, src/test/fixtures/trace):
 *
 * - Claude Code sends one `usage_update` per turn (the SDK's result message). Its tokens are the turn's own
 *   (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`, apart from each other;
 *   thinking is counted in output, `output_tokens_details.thinking_tokens`), so turns add up. Its cost
 *   (`cost.amount`) is the session's so far, so the latest one is the run's.
 * - opencode sends one per step (`step-finish`): `input`, `output`, `reasoning` and `cache.read`, `cache.write`, apart
 *   from each other, and the step's cost. Steps add up, tokens and cost alike.
 * - Codex sends the thread's total so far (`usage`, camelCase) with the last turn's beside it (`last`): the latest is
 *   the run's. Its `inputTokens` include `cachedInputTokens`, and `totalTokens` is input plus output, so reasoning is
 *   read as a part of output. It reports no cost.
 *
 * The run's own `usage` is the adapter's outcome: Claude Code's turns added up with the session's `total_cost_usd`,
 * opencode's steps added up (`cache_read`, `cache_write`, `cost`), Codex's total in snake_case.
 */

export type Cost = { amount: number; currency: string };

export type UsageFigures = {
  cacheRead: number;
  input: number;
  output: number;
  reasoning: number;
  cacheWrite: number;
  cost: Cost | null;
};

/** The four parts of the bar, in the kit's order (chart-5, chart-1, chart-3, chart-4). */
export const USAGE_PARTS = ["cacheRead", "input", "output", "reasoning"] as const;
export type UsagePart = (typeof USAGE_PARTS)[number];

type Shape = "claude" | "codex" | "opencode";
type Body = Record<string, unknown>;

function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

function has(record: Body, ...keys: string[]): boolean {
  return keys.some((key) => typeof record[key] === "number");
}

function shapeOf(record: Body): Shape | null {
  if (has(record, "inputTokens", "outputTokens", "cachedInputTokens")) return "codex";
  if (has(record, "cached_input_tokens", "reasoning_output_tokens")) return "codex";
  if (has(record, "input_tokens", "output_tokens", "cache_read_input_tokens")) return "claude";
  if (has(record, "input", "output", "reasoning", "cache_read") || isRecord(record.cache)) return "opencode";
  return null;
}

/** One report's tokens, in the card's parts, or null for a body of no known shape. */
export function readTokens(record: unknown): Omit<UsageFigures, "cost"> | null {
  if (!isRecord(record)) return null;
  const shape = shapeOf(record);
  if (shape === "claude") {
    const details = isRecord(record.output_tokens_details) ? record.output_tokens_details : {};
    const output = num(record.output_tokens);
    const reasoning = Math.min(num(details.thinking_tokens), output);
    return {
      cacheRead: num(record.cache_read_input_tokens),
      input: num(record.input_tokens),
      output: output - reasoning,
      reasoning,
      cacheWrite: num(record.cache_creation_input_tokens),
    };
  }
  if (shape === "codex") {
    const pick = (camel: string, snake: string) => num(record[camel] ?? record[snake]);
    const input = pick("inputTokens", "input_tokens");
    const cached = Math.min(pick("cachedInputTokens", "cached_input_tokens"), input);
    const output = pick("outputTokens", "output_tokens");
    const reasoning = Math.min(pick("reasoningOutputTokens", "reasoning_output_tokens"), output);
    return { cacheRead: cached, input: input - cached, output: output - reasoning, reasoning, cacheWrite: pick("cacheWriteInputTokens", "cache_write_input_tokens") };
  }
  if (shape === "opencode") {
    const cache = isRecord(record.cache) ? record.cache : {};
    return {
      cacheRead: num(record.cache_read ?? cache.read),
      input: num(record.input),
      output: num(record.output),
      reasoning: num(record.reasoning),
      cacheWrite: num(record.cache_write ?? cache.write),
    };
  }
  return null;
}

function readCost(value: unknown, currency = "USD"): Cost | null {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) return { amount: value, currency };
  if (isRecord(value) && typeof value.amount === "number" && Number.isFinite(value.amount) && value.amount >= 0) {
    return { amount: value.amount, currency: typeof value.currency === "string" && value.currency ? value.currency : currency };
  }
  return null;
}

/** The run's `usage`, as the worker reported it with the run's end. */
export function readRunUsage(usage: unknown): UsageFigures | null {
  const tokens = readTokens(usage);
  if (!tokens || !isRecord(usage)) return null;
  return { ...tokens, cost: readCost(usage.total_cost_usd) ?? readCost(usage.cost) };
}

const ZERO: Omit<UsageFigures, "cost"> = { cacheRead: 0, input: 0, output: 0, reasoning: 0, cacheWrite: 0 };

function add(a: Omit<UsageFigures, "cost">, b: Omit<UsageFigures, "cost">): Omit<UsageFigures, "cost"> {
  return {
    cacheRead: a.cacheRead + b.cacheRead,
    input: a.input + b.input,
    output: a.output + b.output,
    reasoning: a.reasoning + b.reasoning,
    cacheWrite: a.cacheWrite + b.cacheWrite,
  };
}

/** The run's usage so far from its `usage_update` events: per turn or step added up, a running total taken as it is. */
export function usageFromEvents(events: readonly Pick<RunEvent, "seq" | "kind" | "body">[]): UsageFigures | null {
  const reports = events.filter((event) => event.kind === "usage_update").sort((a, b) => a.seq - b.seq);
  let tokens = null as Omit<UsageFigures, "cost"> | null;
  let cost = null as Cost | null;
  for (const report of reports) {
    const body = isRecord(report.body) ? report.body : {};
    const usage = isRecord(body.usage) ? body.usage : body;
    const shape = shapeOf(usage);
    const found = readTokens(usage);
    if (!found) continue;
    const reported = readCost(body.cost);
    if (shape === "codex") {
      tokens = found; // the thread's total so far
      cost = reported ?? cost;
    } else if (shape === "claude") {
      tokens = add(tokens ?? ZERO, found);
      cost = reported ?? cost; // the session's cost so far
    } else {
      tokens = add(tokens ?? ZERO, found);
      cost = reported ? { amount: (cost?.amount ?? 0) + reported.amount, currency: reported.currency } : cost;
    }
  }
  return tokens ? { ...tokens, cost } : null;
}

export type RunUsage = { figures: UsageFigures; source: "run" | "events" } | null;

/** What the card shows: the run's reported usage once it has one, else what its events add up to so far. */
export function runUsage(run: { usage: unknown }, events: readonly Pick<RunEvent, "seq" | "kind" | "body">[]): RunUsage {
  const reported = readRunUsage(run.usage);
  if (reported) return { figures: reported, source: "run" };
  const live = usageFromEvents(events);
  return live ? { figures: live, source: "events" } : null;
}

/** Tokens in the bar: cache read, input, output and reasoning. Cache writes are said beside it. */
export function totalTokens(figures: UsageFigures): number {
  return figures.cacheRead + figures.input + figures.output + figures.reasoning;
}

/** Each part's share of the total, in percent with one decimal; 0 for an empty total. */
export function shares(figures: UsageFigures): Record<UsagePart, number> {
  const total = totalTokens(figures);
  const share = (value: number) => (total > 0 ? Math.round((value / total) * 1000) / 10 : 0);
  return { cacheRead: share(figures.cacheRead), input: share(figures.input), output: share(figures.output), reasoning: share(figures.reasoning) };
}
