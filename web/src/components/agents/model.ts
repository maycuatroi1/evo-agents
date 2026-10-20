import { type Agent, type AgentCreate, type AgentEdit, type AgentPermissions, AXES, type Axis, axisRule } from "@/lib/agents";
import { COLOR, HANDLE, MAX_ALLOW, MAX_DESCRIPTION_CHARS, MAX_HANDLE_CHARS, MAX_INSTRUCTIONS_CHARS, MAX_NAME_CHARS, MAX_PACKS, normalizedItem, validItem } from "@/lib/agent-rules";

/**
 * The agent form's state and the checks it repeats from the API (`agent_profiles.AgentCreate` and `AgentEdit`), so
 * nothing goes out while a field is wrong: a handle of lowercase letters, digits and hyphens that is no project's
 * name, a name of one line, the lengths, the colour, and allowlists of at most MAX_ALLOW items of the shape each axis
 * takes, and at most MAX_PACKS agent packs. The hub checks again and has the last word (a handle taken by another
 * project's agent is its 409, a pack whose plugin was turned off its 422).
 */
export type AxisDraft = { mode: "all" | "allowlist"; allow: string[] };
/** An agent pack the agent attaches: pack `name` of plugin `plugin`, pinned at version `version` of the plugin. */
export type PackPin = { plugin: string; name: string; version: string };
export type AgentDraft = {
  handle: string;
  name: string;
  description: string;
  instructions: string;
  /** null: the hub picks one of its palette by the handle (creating only). */
  color: string | null;
  permissions: Record<Axis, AxisDraft>;
  /** In the order the agent attaches them, each pack once. */
  packs: PackPin[];
};

export type FieldProblem = "blank" | "pattern" | "long" | "line" | "project";
export type DraftProblems = {
  handle?: FieldProblem;
  name?: FieldProblem;
  description?: FieldProblem;
  instructions?: FieldProblem;
  color?: FieldProblem;
  packs?: "tooMany";
  axes: Partial<Record<Axis, "tooMany" | "item">>;
};

const chars = (text: string) => [...text].length;
const CONTROL = /[\u0000-\u001f\u007f]/;

function allAxes(): Record<Axis, AxisDraft> {
  const all = (): AxisDraft => ({ mode: "all", allow: [] });
  return { credential: all(), tool: all(), skill: all(), worker: all(), project: all(), model: all(), runtime: all(), environment: all() };
}

export function emptyDraft(): AgentDraft {
  return { handle: "", name: "", description: "", instructions: "", color: null, permissions: allAxes(), packs: [] };
}

export function draftOf(agent: Agent): AgentDraft {
  const permissions = Object.fromEntries(
    AXES.map((axis) => {
      const rule = axisRule(agent.permissions, axis);
      return [axis, { mode: rule.mode, allow: rule.mode === "allowlist" ? [...(rule.allow ?? [])] : [] }];
    }),
  ) as Record<Axis, AxisDraft>;
  return {
    handle: agent.handle,
    name: agent.name,
    description: agent.description,
    instructions: agent.instructions,
    color: agent.color,
    permissions,
    packs: (agent.packs ?? []).map(({ plugin, name, version }) => ({ plugin, name, version })),
  };
}

/** `<plugin>/<name>`, how the hub names a pack. */
export const packKey = (pin: Pick<PackPin, "plugin" | "name">) => `${pin.plugin}/${pin.name}`;

/** The packs with `pin` attached last, or its version changed where it is attached already. */
export function withPack(packs: readonly PackPin[], pin: PackPin): PackPin[] {
  const key = packKey(pin);
  return packs.some((other) => packKey(other) === key) ? packs.map((other) => (packKey(other) === key ? { ...pin } : other)) : [...packs, { ...pin }];
}

export function withoutPack(packs: readonly PackPin[], key: string): PackPin[] {
  return packs.filter((other) => packKey(other) !== key);
}

function samePacks(a: readonly PackPin[], b: readonly PackPin[]): boolean {
  return a.length === b.length && a.every((pin, index) => packKey(pin) === packKey(b[index]) && pin.version === b[index].version);
}

/** Add `item` to an allowlist as the hub keeps it: normalized, once. */
export function withItem(draft: AxisDraft, axis: Axis, item: string): AxisDraft {
  const value = normalizedItem(axis, item);
  if (!value || draft.allow.includes(value)) return draft;
  return { ...draft, allow: [...draft.allow, value] };
}

export function withoutItem(draft: AxisDraft, item: string): AxisDraft {
  return { ...draft, allow: draft.allow.filter((other) => other !== item) };
}

export function draftProblems(draft: AgentDraft, { creating, projects }: { creating: boolean; projects: readonly string[] }): DraftProblems {
  const problems: DraftProblems = { axes: {} };
  if (creating) {
    const handle = draft.handle.trim();
    if (!handle) problems.handle = "blank";
    else if (chars(handle) > MAX_HANDLE_CHARS) problems.handle = "long";
    else if (!HANDLE.test(handle)) problems.handle = "pattern";
    else if (projects.includes(handle)) problems.handle = "project";
  }
  const name = draft.name.trim();
  if (!name) problems.name = "blank";
  else if (chars(name) > MAX_NAME_CHARS) problems.name = "long";
  else if (CONTROL.test(name)) problems.name = "line";
  if (chars(draft.description) > MAX_DESCRIPTION_CHARS) problems.description = "long";
  if (chars(draft.instructions) > MAX_INSTRUCTIONS_CHARS) problems.instructions = "long";
  if (draft.color !== null && !COLOR.test(draft.color)) problems.color = "pattern";
  if (draft.packs.length > MAX_PACKS) problems.packs = "tooMany";
  for (const axis of AXES) {
    const rule = draft.permissions[axis];
    if (rule.mode !== "allowlist") continue;
    if (rule.allow.length > MAX_ALLOW) problems.axes[axis] = "tooMany";
    else if (rule.allow.some((item) => !validItem(axis, item))) problems.axes[axis] = "item";
  }
  return problems;
}

export function hasProblems(problems: DraftProblems): boolean {
  const { axes, ...fields } = problems;
  return Object.values(fields).some(Boolean) || Object.values(axes).some(Boolean);
}

/** The eight axes as the API takes them: `{mode: "all"}`, or the mode with its list. */
export function permissionsBody(permissions: Record<Axis, AxisDraft>): AgentPermissions {
  return Object.fromEntries(
    AXES.map((axis) => {
      const rule = permissions[axis];
      return [axis, rule.mode === "allowlist" ? { mode: "allowlist", allow: [...rule.allow] } : { mode: "all" }];
    }),
  ) as AgentPermissions;
}

function samePermissions(a: Record<Axis, AxisDraft>, b: Record<Axis, AxisDraft>): boolean {
  return AXES.every((axis) => {
    const left = a[axis];
    const right = b[axis];
    if (left.mode !== right.mode) return false;
    return left.mode === "all" || (left.allow.length === right.allow.length && left.allow.every((item, index) => item === right.allow[index]));
  });
}

export function createBody(draft: AgentDraft): AgentCreate {
  return {
    handle: draft.handle.trim(),
    name: draft.name.trim(),
    description: draft.description.trim(),
    instructions: draft.instructions.trim(),
    ...(draft.color ? { color: draft.color } : {}),
    permissions: permissionsBody(draft.permissions),
    ...(draft.packs.length ? { packs: draft.packs.map((pin) => ({ ...pin })) } : {}),
  };
}

/**
 * The PATCH of an edit: only what changed, against the version the form opened on (`if_version`, so an edit made
 * meanwhile is a 409, not overwritten); null when nothing changed. The packs go whole when any changed. The default
 * agent keeps every permission, no instructions and no pack, so its form sends none of them.
 */
export function editBody(draft: AgentDraft, agent: Agent): AgentEdit | null {
  const before = draftOf(agent);
  const body: AgentEdit = {};
  // Compared trimmed, so text the CLI stored with a trailing line break is not a change by itself.
  const name = draft.name.trim();
  if (name !== before.name.trim()) body.name = name;
  const description = draft.description.trim();
  if (description !== before.description.trim()) body.description = description;
  if (draft.color && draft.color !== before.color) body.color = draft.color;
  if (!agent.is_default) {
    const instructions = draft.instructions.trim();
    if (instructions !== before.instructions.trim()) body.instructions = instructions;
    if (!samePermissions(draft.permissions, before.permissions)) body.permissions = permissionsBody(draft.permissions);
    if (!samePacks(draft.packs, before.packs)) body.packs = draft.packs.map((pin) => ({ ...pin }));
  }
  return Object.keys(body).length ? { ...body, if_version: agent.version } : null;
}

/** What a version changed against the one before it, by the snapshot the hub keeps of each (`AgentVersion.body`). */
export type VersionChange = "name" | "description" | "instructions" | "color" | "avatar" | Axis | "packs";

const SNAPSHOT_FIELDS = [
  ["name", "name"],
  ["description", "description"],
  ["instructions", "instructions"],
  ["color", "color"],
  ["avatar_sha256", "avatar"],
] as const;

function axisOf(body: Record<string, unknown>, axis: Axis): string {
  const permissions = body.permissions;
  const rule = typeof permissions === "object" && permissions !== null ? (permissions as Record<string, unknown>)[axis] : undefined;
  if (typeof rule !== "object" || rule === null) return "all";
  const { mode, allow } = rule as { mode?: unknown; allow?: unknown };
  return mode === "allowlist" ? `allowlist:${JSON.stringify(Array.isArray(allow) ? allow : [])}` : "all";
}

/** The packs of a snapshot, compared whole; a version from before the packs has none. */
const packsOf = (body: Record<string, unknown>) => JSON.stringify(Array.isArray(body.packs) ? body.packs : []);

export function versionChanges(newer: Record<string, unknown>, older: Record<string, unknown>): VersionChange[] {
  const changes: VersionChange[] = SNAPSHOT_FIELDS.filter(([key]) => (newer[key] ?? null) !== (older[key] ?? null)).map(([, change]) => change);
  const packs: VersionChange[] = packsOf(newer) === packsOf(older) ? [] : ["packs"];
  return [...changes, ...AXES.filter((axis) => axisOf(newer, axis) !== axisOf(older, axis)), ...packs];
}
