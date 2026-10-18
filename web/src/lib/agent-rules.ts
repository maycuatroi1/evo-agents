import { type Agent, type Axis, LABEL_PREFIX } from "@/lib/agents";

/**
 * What the Agents pages check and draw, beside lib/agents.ts (which the dispatch forms load with every plans and runs
 * page, so it keeps to what they need): the API's words and limits for a profile's fields and allowlists, the shape
 * each axis takes, the hub's palette, and an avatar's initial and the colour it is written in.
 */
/** `profiles.TOOLS`: the runtime-neutral words of the tool axis; `mcp:<server>` names the tools of one MCP server. */
export const TOOLS = ["read", "edit", "bash", "web", "agent"] as const;
export const AGENT_RUNTIMES = ["claude-code", "opencode", "codex"] as const;
export const GITHUB_APP = "github-app";
/** `run_environments.HOST`: the worker's own machine, beside the environments of plugins, `<plugin>/<name>`. */
export const HOST_ENVIRONMENT = "host";
export const MCP_PREFIX = "mcp:";

/** `profiles.HANDLE_PATTERN` and the limits of the API's fields. */
export const HANDLE = /^[a-z0-9][a-z0-9-]{0,99}$/;
export const MAX_HANDLE_CHARS = 100;
export const MAX_NAME_CHARS = 80;
export const MAX_DESCRIPTION_CHARS = 1000;
export const MAX_INSTRUCTIONS_CHARS = 8000;
export const MAX_ALLOW = 200;
export const MAX_MODEL_CHARS = 200;
export const COLOR = /^#[0-9a-f]{6}$/;
/** `profiles.PALETTE`: the colours the hub picks from by handle when a profile is created without one. */
export const PALETTE = ["#2563eb", "#7c3aed", "#db2777", "#dc2626", "#d97706", "#16a34a", "#0891b2", "#4f46e5"] as const;
/** `agent_profiles.AVATAR_MAX_BYTES` and `AVATAR_TYPES`: what PUT .../avatar takes. */
export const AVATAR_MAX_BYTES = 512 * 1024;
export const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

const SECRET = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SKILL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const WORKER = /^[a-z0-9][a-z0-9._-]{0,99}$/;
const LABEL = /^[a-z0-9][a-z0-9._-]{0,39}$/;
const MCP_SERVER = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const LINE = /^[^\u0000-\u001f\u007f]+$/;
const PLUGIN_ENVIRONMENT = /^[a-z][a-z0-9-]{0,62}\/[a-z][a-z0-9_-]{0,62}$/;

/** An allowlist item as the hub keeps it: trimmed, and lowered for the worker axis. */
export function normalizedItem(axis: Axis, item: string): string {
  const trimmed = item.trim();
  return axis === "worker" ? trimmed.toLowerCase() : trimmed;
}

/** Whether `item` (normalized) can be in the allowlist of `axis`, by `profiles.item_problem` of the API. */
export function validItem(axis: Axis, item: string): boolean {
  switch (axis) {
    case "credential":
      return SECRET.test(item);
    case "tool":
      return (TOOLS as readonly string[]).includes(item) || (item.startsWith(MCP_PREFIX) && MCP_SERVER.test(item.slice(MCP_PREFIX.length)));
    case "skill":
      return SKILL.test(item);
    case "worker":
      return item.startsWith(LABEL_PREFIX) ? LABEL.test(item.slice(LABEL_PREFIX.length)) : WORKER.test(item);
    case "project":
      return HANDLE.test(item);
    case "model":
      return LINE.test(item) && [...item].length <= MAX_MODEL_CHARS;
    case "runtime":
      return (AGENT_RUNTIMES as readonly string[]).includes(item);
    case "environment":
      return item === HOST_ENVIRONMENT || PLUGIN_ENVIRONMENT.test(item);
  }
}

/** The letter an avatar without an image shows: the name's first letter or digit, else the handle's. */
export function agentInitial(agent: Pick<Agent, "name" | "handle">): string {
  const letter = [...agent.name.trim()].find((char) => /[\p{L}\p{N}]/u.test(char)) ?? agent.handle.charAt(0);
  return letter.toLocaleUpperCase();
}

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

const WHITE = "#ffffff";
const INK = "#0e1116"; // the kit's fg in light

/** The colour of an initial on `color`: white or ink, whichever stands out more (WCAG contrast). */
export function inkOn(color: string): string {
  if (!COLOR.test(color)) return WHITE;
  const ground = luminance(color);
  const onWhite = 1.05 / (ground + 0.05);
  const onInk = (ground + 0.05) / (luminance(INK) + 0.05);
  return onWhite >= onInk ? WHITE : INK;
}

