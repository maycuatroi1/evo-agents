import { type Agent, type AgentCreate, type AgentEdit, type AgentPermissions, AXES, type Axis, axisRule } from "@/lib/agents";
import { AVATAR_MAX_BYTES, AVATAR_TYPES, COLOR, HANDLE, MAX_ALLOW, MAX_DESCRIPTION_CHARS, MAX_HANDLE_CHARS, MAX_INSTRUCTIONS_CHARS, MAX_NAME_CHARS, normalizedItem, validItem } from "@/lib/agent-rules";

/**
 * The agent form's state and the checks it repeats from the API (`agent_profiles.AgentCreate` and `AgentEdit`), so
 * nothing goes out while a field is wrong: a handle of lowercase letters, digits and hyphens that is no project's
 * name, a name of one line, the lengths, the colour, and allowlists of at most MAX_ALLOW items of the shape each axis
 * takes. The hub checks again and has the last word (a handle taken by another project's agent is its 409).
 */
export type AxisDraft = { mode: "all" | "allowlist"; allow: string[] };
export type AgentDraft = {
  handle: string;
  name: string;
  description: string;
  instructions: string;
  /** null: the hub picks one of its palette by the handle (creating only). */
  color: string | null;
  permissions: Record<Axis, AxisDraft>;
};

export type FieldProblem = "blank" | "pattern" | "long" | "line" | "project";
export type DraftProblems = {
  handle?: FieldProblem;
  name?: FieldProblem;
  description?: FieldProblem;
  instructions?: FieldProblem;
  color?: FieldProblem;
  axes: Partial<Record<Axis, "tooMany" | "item">>;
};

const chars = (text: string) => [...text].length;
const CONTROL = /[\u0000-\u001f\u007f]/;

function allAxes(): Record<Axis, AxisDraft> {
  const all = (): AxisDraft => ({ mode: "all", allow: [] });
  return { credential: all(), tool: all(), skill: all(), worker: all(), project: all(), model: all(), runtime: all() };
}

export function emptyDraft(): AgentDraft {
  return { handle: "", name: "", description: "", instructions: "", color: null, permissions: allAxes() };
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
  };
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

/** The seven axes as the API takes them: `{mode: "all"}`, or the mode with its list. */
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
  };
}

/**
 * The PATCH of an edit: only what changed, against the version the form opened on (`if_version`, so an edit made
 * meanwhile is a 409, not overwritten); null when nothing changed. The default agent keeps every permission and no
 * instructions, so its form sends neither.
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
  }
  return Object.keys(body).length ? { ...body, if_version: agent.version } : null;
}

/** Why an image cannot be an avatar before it is sent: its type, or its size. */
export function avatarProblem(file: Pick<File, "type" | "size">): "type" | "size" | null {
  if (!(AVATAR_TYPES as readonly string[]).includes(file.type)) return "type";
  if (file.size > AVATAR_MAX_BYTES) return "size";
  return null;
}

/** The bytes of `file` in base64, as PUT .../avatar takes them. */
export async function fileBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  const CHUNK = 0x8000;
  for (let start = 0; start < bytes.length; start += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(start, start + CHUNK));
  }
  return btoa(binary);
}

/** What a version changed against the one before it, by the snapshot the hub keeps of each (`AgentVersion.body`). */
export type VersionChange = "name" | "description" | "instructions" | "color" | "avatar" | Axis;

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

export function versionChanges(newer: Record<string, unknown>, older: Record<string, unknown>): VersionChange[] {
  const changes: VersionChange[] = SNAPSHOT_FIELDS.filter(([key]) => (newer[key] ?? null) !== (older[key] ?? null)).map(([, change]) => change);
  return [...changes, ...AXES.filter((axis) => axisOf(newer, axis) !== axisOf(older, axis))];
}
