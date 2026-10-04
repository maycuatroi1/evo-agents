import { BookMarked, FolderKanban, type LucideIcon, MessageSquareQuote, UserRound } from "lucide-react";

/**
 * What the memories pages read out of a memory: its frontmatter, its type and its label. A memory is a whole
 * Claude Code memory file, frontmatter included (`evo_agents.hub.memory`).
 */

export const MEMORY_TYPES = ["project", "reference", "user", "feedback"] as const;
export type MemoryType = (typeof MEMORY_TYPES)[number];

/** Types a project's members share; the others only their owner sees (`SHARED_TYPES` on the hub). */
export const SHARED_TYPES: ReadonlySet<string> = new Set(["project", "reference"]);

export const TYPE_ICONS: Record<MemoryType, LucideIcon> = {
  project: FolderKanban,
  reference: BookMarked,
  user: UserRound,
  feedback: MessageSquareQuote,
};

export function isMemoryType(value: string): value is MemoryType {
  return (MEMORY_TYPES as readonly string[]).includes(value);
}

// The hub's FRONTMATTER: a block between two --- lines at the very start.
const FRONTMATTER = /^---[ \t]*\r?\n([\s\S]*?\r?\n)?---[ \t]*(?:\r?\n|$)/;
const FIELD = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/;

export type Frontmatter = {
  /** The frontmatter as written, without its --- lines; null when the file has none. */
  raw: string | null;
  /** Top-level `key: value` lines with a plain value, quotes removed. */
  fields: Record<string, string>;
  /** The file after the frontmatter. */
  body: string;
};

function unquote(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2 && (trimmed[0] === '"' || trimmed[0] === "'") && trimmed.at(-1) === trimmed[0]) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

export function parseFrontmatter(text: string): Frontmatter {
  const match = FRONTMATTER.exec(text);
  if (!match) return { raw: null, fields: {}, body: text };
  const raw = (match[1] ?? "").replace(/\r?\n$/, "");
  const fields: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const field = FIELD.exec(line);
    if (!field) continue;
    const value = unquote(field[2]);
    // A key with nothing after it opens a block (metadata:), and |, > start multi-line text: neither is a value.
    if (value && value !== "|" && value !== ">" && !(field[1] in fields)) fields[field[1]] = value;
  }
  return { raw, fields, body: text.slice(match[0].length) };
}

/** The title and summary a memory file gives itself, falling back to its file name. */
export function memoryTitle(name: string, body: string): { title: string; description: string | null } {
  const { fields } = parseFrontmatter(body);
  return { title: fields.name || name, description: fields.description || null };
}

/** A stored label as names: {level, location, integrity, projects}; anything else counts as missing. */
export type LabelNames = { level: string | null; location: string | null; integrity: string | null };

export function labelNames(label: Record<string, unknown>): LabelNames {
  const text = (key: string) => {
    const value = label[key];
    return typeof value === "string" && value.length > 0 ? value : null;
  };
  return { level: text("level"), location: text("location"), integrity: text("integrity") };
}

