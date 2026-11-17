/**
 * The Token strategy of a project as the web reads and writes it, without React: the section `routing` of its
 * autonomy policy (`autonomy.ROUTING_KEYS`, docs/autonomy.md). Off by default; on, the hub rates a run that names no
 * model easy, medium or hard and gives it the ladder's model of that difficulty, or the overflow's (a cheaper gateway
 * model) once the subscription's weekly quota is down to its reserve or its five-hour window past its ceiling, as the
 * strategy says. The hub checks every write again.
 */

export const ROUTING_STRATEGIES = ["surplus-first", "cost-first", "quota-burn"] as const;
export type RoutingStrategy = (typeof ROUTING_STRATEGIES)[number];
export const LEVELS = ["easy", "medium", "hard"] as const;
export type Level = (typeof LEVELS)[number];
/** The models of the native runtime (Claude Code) the ladder names without a prefix. */
export const NATIVE_MODELS = ["haiku", "sonnet", "opus"] as const;
/** The scorer's value that turns it off: a free prompt the rules cannot rate is then medium. */
export const SCORER_OFF = "off";
export const MAX_MODEL_CHARS = 200;
const GATEWAY_MODEL = /^[a-z0-9][a-z0-9_-]{0,31}\/[^/\s]\S*$/;
export const RESERVE = { min: 0, max: 100 };
export const CEILING = { min: 1, max: 100 };

export type Routing = {
  enabled: boolean;
  strategy: RoutingStrategy;
  hard_reserve_pct: number;
  five_hour_ceiling_pct: number;
  scorer: string;
  ladder: Record<Level, string>;
  overflow: Record<Level, string | null>;
};

export const ROUTING_DEFAULTS: Routing = {
  enabled: false,
  strategy: "surplus-first",
  hard_reserve_pct: 10,
  five_hour_ceiling_pct: 90,
  scorer: "haiku",
  ladder: { easy: "haiku", medium: "sonnet", hard: "opus" },
  overflow: { easy: null, medium: null, hard: null },
};

type Tree = Record<string, unknown>;
const isTree = (value: unknown): value is Tree => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, fallback: string) => (typeof value === "string" && value !== "" ? value : fallback);
const whole = (value: unknown, fallback: number) => (typeof value === "number" && Number.isInteger(value) ? value : fallback);

/** The section `routing` of a policy (its body or its effective values), each key at its value or its default. */
export function readRouting(policy: Tree): Routing {
  const routing = isTree(policy.routing) ? policy.routing : {};
  const ladder = isTree(routing.ladder) ? routing.ladder : {};
  const overflow = isTree(routing.overflow) ? routing.overflow : {};
  const strategy = (ROUTING_STRATEGIES as readonly unknown[]).includes(routing.strategy) ? (routing.strategy as RoutingStrategy) : ROUTING_DEFAULTS.strategy;
  return {
    enabled: routing.enabled === true,
    strategy,
    hard_reserve_pct: whole(routing.hard_reserve_pct, ROUTING_DEFAULTS.hard_reserve_pct),
    five_hour_ceiling_pct: whole(routing.five_hour_ceiling_pct, ROUTING_DEFAULTS.five_hour_ceiling_pct),
    scorer: text(routing.scorer, ROUTING_DEFAULTS.scorer),
    ladder: Object.fromEntries(LEVELS.map((level) => [level, text(ladder[level], ROUTING_DEFAULTS.ladder[level])])) as Record<Level, string>,
    overflow: Object.fromEntries(LEVELS.map((level) => [level, typeof overflow[level] === "string" ? overflow[level] : null])) as Record<Level, string | null>,
  };
}

export type RoutingForm = {
  enabled: boolean;
  strategy: RoutingStrategy;
  reservePct: string;
  ceilingPct: string;
  scorer: string;
  ladder: Record<Level, string>;
  overflow: Record<Level, string>;
};

export const routingFormOf = (routing: Routing): RoutingForm => ({
  enabled: routing.enabled,
  strategy: routing.strategy,
  reservePct: String(routing.hard_reserve_pct),
  ceilingPct: String(routing.five_hour_ceiling_pct),
  scorer: routing.scorer,
  ladder: { ...routing.ladder },
  overflow: { easy: routing.overflow.easy ?? "", medium: routing.overflow.medium ?? "", hard: routing.overflow.hard ?? "" },
});

export type RoutingProblem = { code: "whole" | "range"; min: number; max: number } | { code: "required" } | { code: "model"; max: number };

/** Why a model name is wrong, as `autonomy._model` says: one line of text, and `<prefix>/<model>` when it has a slash. */
export function modelProblem(value: string, optional = false): RoutingProblem | null {
  const name = value.trim();
  if (name === "") return optional ? null : { code: "required" };
  if (name.length > MAX_MODEL_CHARS || /\s/.test(name) || (name.includes("/") && !GATEWAY_MODEL.test(name))) return { code: "model", max: MAX_MODEL_CHARS };
  return null;
}

function percentProblem(value: string, bounds: { min: number; max: number }): RoutingProblem | null {
  if (!/^\d+$/.test(value.trim())) return { code: "whole", ...bounds };
  const number = Number(value.trim());
  return number < bounds.min || number > bounds.max ? { code: "range", ...bounds } : null;
}

/** The problems of the form by field: `reservePct`, `ceilingPct`, `scorer`, `ladder.<level>`, `overflow.<level>`. */
export function routingProblems(form: RoutingForm): Record<string, RoutingProblem> {
  const found: Record<string, RoutingProblem | null> = {
    reservePct: percentProblem(form.reservePct, RESERVE),
    ceilingPct: percentProblem(form.ceilingPct, CEILING),
    scorer: modelProblem(form.scorer),
  };
  for (const level of LEVELS) {
    found[`ladder.${level}`] = modelProblem(form.ladder[level]);
    found[`overflow.${level}`] = modelProblem(form.overflow[level], true);
  }
  return Object.fromEntries(Object.entries(found).filter((entry): entry is [string, RoutingProblem] => entry[1] !== null));
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * The section `routing` the form makes of `held` (the policy's newest body): as the rest of the policy, it keeps a key
 * the body set and the form left, adds one the form changed from its default and drops one put back to its default.
 * Null when it holds no key. The form must have no problem.
 */
export function routingSection(form: RoutingForm, held: Tree): Tree | null {
  const before = isTree(held.routing) ? held.routing : {};
  const values: [string, unknown, unknown][] = [
    ["enabled", form.enabled, ROUTING_DEFAULTS.enabled],
    ["strategy", form.strategy, ROUTING_DEFAULTS.strategy],
    ["hard_reserve_pct", Number(form.reservePct.trim()), ROUTING_DEFAULTS.hard_reserve_pct],
    ["five_hour_ceiling_pct", Number(form.ceilingPct.trim()), ROUTING_DEFAULTS.five_hour_ceiling_pct],
    ["scorer", form.scorer.trim(), ROUTING_DEFAULTS.scorer],
  ];
  const section: Tree = {};
  const keep = (into: Tree, key: string, value: unknown, fallback: unknown, was: unknown) => {
    if ((was !== undefined && same(value, was)) || !same(value, fallback)) into[key] = value;
  };
  for (const [key, value, fallback] of values) keep(section, key, value, fallback, before[key]);
  for (const group of ["ladder", "overflow"] as const) {
    const was = isTree(before[group]) ? before[group] : {};
    const own: Tree = {};
    for (const level of LEVELS) {
      const value = form[group][level].trim();
      keep(own, level, group === "overflow" && value === "" ? null : value, ROUTING_DEFAULTS[group][level], was[level]);
    }
    if (Object.keys(own).length > 0) section[group] = own;
  }
  return Object.keys(section).length > 0 ? section : null;
}

/** `policy` (a whole body) with `routing` as its section, or without one when it is null. */
export function withRouting(policy: Tree, routing: Tree | null): Tree {
  const rest = { ...policy };
  delete rest.routing;
  return routing ? { ...rest, routing } : rest;
}

/** The model suggestions of a field: the native runtime's own, then the gateway models the project may run. */
export function modelSuggestions(runnable: readonly { model: string }[]): string[] {
  return [...new Set([...NATIVE_MODELS, ...runnable.map((item) => item.model)])];
}
