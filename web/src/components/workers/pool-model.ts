import type { JoinToken, Pool, PoolNode, PoolRequest, PoolUpdate, Worker } from "./queries";

/**
 * How the web reads and writes a worker pool (docs/workers.md, Pools): the checks of its form, which repeat the hub's
 * (`worker_pool_models.py`) so a mistake is caught before it is sent, the body a save sends, what about a pool or a
 * join token needs a person, and the command that turns a fresh Ubuntu machine into a node. Pure functions.
 */

/** `worker_pool_models.POOL_NAME`. */
export const POOL_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
/** `worker_pool_models.POOL_LABEL`: a word, or key=value. */
export const POOL_LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._-]{0,59})?$/;
/** `runs.models.placement.PLACEMENT_LABEL`: a worker's own taint. */
export const TAINT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?$/;
/** `worker_doctor.CODE_PATTERN`: the code of a doctor finding. */
export const FINDING_CODE = /^[a-z][a-z0-9-]{0,62}$/;
/** The keys the hub gives labels itself, which a pool may not set. */
export const RESERVED_KEYS = ["pool", "os", "arch", "runtime", "feature"] as const;
export const MAX_POOL_LABELS = 16;
export const MAX_ACCEPTED = 32;
export const REAP_DAYS = { min: 1, max: 90 } as const;
export const TOKEN_HOURS = { min: 1, max: 720 } as const;
export const MAX_USES = { min: 1, max: 100 } as const;
/** A live join token this close to its end is said to expire soon. */
export const EXPIRING_SOON_MS = 2 * 60 * 60 * 1000;

/**
 * Words typed in one field, split at commas, spaces and line breaks, each once. `lower` lowercases them, as the hub
 * keeps a worker's labels and taints; a pool keeps them as they were typed.
 */
export function words(input: string, lower = false): string[] {
  return [...new Set(input.split(/[\s,]+/).map((word) => (lower ? word.trim().toLowerCase() : word.trim())).filter(Boolean))];
}

export type ListInput = { values: string[]; invalid: string[]; reserved: string[]; tooMany: boolean };

/** A list of labels or taints: those the pattern takes, those it does not, and those that use a reserved key. */
export function parseList(input: string, pattern: RegExp, max: number, { reserve = false, lower = false } = {}): ListInput {
  const all = words(input, lower);
  const invalid = all.filter((word) => !pattern.test(word));
  const reserved = reserve
    ? all.filter((word) => pattern.test(word) && (RESERVED_KEYS as readonly string[]).includes(word.split("=")[0].toLowerCase()))
    : [];
  const values = all.filter((word) => pattern.test(word) && !reserved.includes(word));
  return { values, invalid, reserved, tooMany: values.length > max };
}

export type PoolForm = {
  name: string;
  projects: string[];
  slots: string;
  labels: string;
  taints: string;
  accepted: string;
  reapDays: string;
};

export type PoolField = keyof PoolForm;
export const POOL_FIELDS: readonly PoolField[] = ["name", "projects", "slots", "labels", "taints", "accepted", "reapDays"];

export type PoolProblem =
  | { code: "nameRequired" }
  | { code: "nameInvalid" }
  | { code: "nameTaken" }
  | { code: "projectsRequired" }
  | { code: "number"; min: number; max: number }
  | { code: "invalid"; values: string }
  | { code: "reserved"; values: string }
  | { code: "tooMany"; max: number };

export type PoolErrors = Partial<Record<PoolField, PoolProblem>>;

export function emptyPoolForm(): PoolForm {
  return { name: "", projects: [], slots: "1", labels: "", taints: "", accepted: "", reapDays: "7" };
}

export function poolForm(pool: Pool): PoolForm {
  return {
    name: pool.name,
    projects: [...pool.projects].sort(),
    slots: String(pool.slots),
    labels: pool.labels.join(", "),
    taints: pool.taints.join(", "),
    accepted: pool.accepted_findings.join(", "),
    reapDays: String(pool.reap_after_days),
  };
}

function wholeNumber(text: string, min: number, max: number): number | null {
  const value = Number(text.trim());
  return text.trim() !== "" && Number.isInteger(value) && value >= min && value <= max ? value : null;
}

function listProblem(list: ListInput, max: number): PoolProblem | null {
  if (list.invalid.length) return { code: "invalid", values: list.invalid.join(", ") };
  if (list.reserved.length) return { code: "reserved", values: list.reserved.join(", ") };
  if (list.tooMany) return { code: "tooMany", max };
  return null;
}

/** The pool a form describes, or the first problem of each field. `maxSlots` is the hub's `runs.MAX_SLOTS`. */
export function readPoolForm(form: PoolForm, maxSlots: number): { pool: PoolRequest; errors: null } | { pool: null; errors: PoolErrors } {
  const errors: PoolErrors = {};
  const name = form.name.trim();
  if (!name) errors.name = { code: "nameRequired" };
  else if (!POOL_NAME.test(name)) errors.name = { code: "nameInvalid" };
  if (form.projects.length === 0) errors.projects = { code: "projectsRequired" };
  const slots = wholeNumber(form.slots, 1, maxSlots);
  if (slots === null) errors.slots = { code: "number", min: 1, max: maxSlots };
  const labels = parseList(form.labels, POOL_LABEL, MAX_POOL_LABELS, { reserve: true });
  const taints = parseList(form.taints, POOL_LABEL, MAX_POOL_LABELS, { reserve: true });
  const accepted = parseList(form.accepted, FINDING_CODE, MAX_ACCEPTED);
  for (const [field, list, max] of [
    ["labels", labels, MAX_POOL_LABELS],
    ["taints", taints, MAX_POOL_LABELS],
    ["accepted", accepted, MAX_ACCEPTED],
  ] as const) {
    const problem = listProblem(list, max);
    if (problem) errors[field] = problem;
  }
  const reapDays = wholeNumber(form.reapDays, REAP_DAYS.min, REAP_DAYS.max);
  if (reapDays === null) errors.reapDays = { code: "number", min: REAP_DAYS.min, max: REAP_DAYS.max };
  if (Object.keys(errors).length > 0) return { pool: null, errors };
  return {
    pool: {
      name,
      projects: [...form.projects].sort(),
      slots: slots as number,
      labels: labels.values,
      taints: taints.values,
      accepted_findings: accepted.values,
      reap_after_days: reapDays as number,
    },
    errors: null,
  };
}

const same = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((value, index) => value === b[index]);

/** What a save of `next` changes on `pool`: only the fields that differ; null when nothing does. */
export function poolChanges(pool: Pool, next: PoolRequest): PoolUpdate | null {
  const body: PoolUpdate = {};
  if (!same([...pool.projects].sort(), next.projects)) body.projects = next.projects;
  if (next.slots !== pool.slots) body.slots = next.slots;
  if (!same(pool.labels, next.labels ?? [])) body.labels = next.labels;
  if (!same(pool.taints, next.taints ?? [])) body.taints = next.taints;
  if (!same(pool.accepted_findings, next.accepted_findings ?? [])) body.accepted_findings = next.accepted_findings;
  if (next.reap_after_days !== pool.reap_after_days) body.reap_after_days = next.reap_after_days;
  return Object.keys(body).length > 0 ? body : null;
}

/** The nodes of a pool that need a person: offline or draining, or not ready (their doctor found something). */
export function nodeProblem(node: Pick<PoolNode, "status" | "ready">): "notReady" | "offline" | "draining" | null {
  if (node.status === "offline") return "offline";
  if (node.status === "draining") return "draining";
  if (!node.ready) return "notReady";
  return null;
}

export type PoolHealth = { nodes: number; ready: number; notReady: number; offline: number };

/** A pool's nodes at a glance: how many, how many take runs, how many the doctor holds back, how many are offline. */
export function poolHealth(pool: Pick<Pool, "nodes">): PoolHealth {
  const health: PoolHealth = { nodes: pool.nodes.length, ready: 0, notReady: 0, offline: 0 };
  for (const node of pool.nodes) {
    if (node.status === "offline") health.offline += 1;
    else if (!node.ready) health.notReady += 1;
    else if (node.status === "online") health.ready += 1;
  }
  return health;
}

/** A join token that still works but ends within EXPIRING_SOON_MS of `now`. */
export function expiringSoon(token: Pick<JoinToken, "status" | "expires_at">, now: number | null): boolean {
  if (token.status !== "live" || now === null) return false;
  const left = Date.parse(token.expires_at) - now;
  return left > 0 && left <= EXPIRING_SOON_MS;
}

/** The URL of deploy/worker/bootstrap.sh that the command downloads. */
export const BOOTSTRAP_URL = "https://raw.githubusercontent.com/maycuatroi1/evo-agents/main/deploy/worker/bootstrap.sh";

/** One shell value, quoted only when it needs it. */
function shellValue(value: string): string {
  return /^[A-Za-z0-9_./:=@+-]+$/.test(value) ? value : `'${value.replaceAll("'", `'"'"'`)}'`;
}

/**
 * The command that makes a fresh Ubuntu machine a node of the pool: it downloads the bootstrap script, which installs
 * the toolchain and the daemon, joins with the token and starts the service (docs/workers.md, Pools).
 */
export function bootstrapCommand(hubUrl: string, token: string): string {
  return `curl -fsSL ${BOOTSTRAP_URL} -o bootstrap.sh && EVO_HUB_URL=${shellValue(hubUrl)} EVO_JOIN_TOKEN=${shellValue(token)} bash bootstrap.sh`;
}

/** The command that prints cloud-init user-data with a new token, for a provider that takes it when it makes a VPS. */
export function userDataCommand(pool: string): string {
  return `evo-agents worker pool user-data ${shellValue(pool)} > user-data.sh`;
}

export type Inherited = { labels: string[]; taints: string[] };

/** What a node has from its pool rather than of its own: the pool's labels with pool=<name>, and the pool's taints. */
export function inheritedOf(pool: Pick<Pool, "name" | "labels" | "taints">): Inherited {
  return { labels: [...pool.labels, `pool=${pool.name}`], taints: [...pool.taints] };
}

/**
 * A worker's own labels and taints, which PATCH /v1/workers/{id} edits: the hub shows its own first and its pool's
 * after them, so the pool's are taken off the end (labels are kept once, so an own label the pool has too is the
 * pool's). A worker of no pool has only its own.
 */
export function ownOf(worker: Pick<Worker, "labels" | "taints">, inherited: Inherited | null): Inherited {
  if (!inherited) return { labels: [...worker.labels], taints: [...worker.taints] };
  const labels = worker.labels.filter((label) => !inherited.labels.includes(label));
  const tail = worker.taints.slice(worker.taints.length - inherited.taints.length);
  const taints = same(tail, inherited.taints)
    ? worker.taints.slice(0, worker.taints.length - inherited.taints.length)
    : worker.taints.filter((taint) => !inherited.taints.includes(taint));
  return { labels, taints };
}
