import type { components } from "./api/schema";

/**
 * The outputs of runs as the web reads them (docs/hub.md, Run outputs): the files a run's agent made that the hub
 * keeps, each with its name, media type, size, caption, source and the URL of its bytes on the hub's own origin. The
 * URL is scoped to what listed the output (the run, a decision, a chat), so a strip shows bytes only to who may read
 * them; `?download=true` asks for them as an attachment. The hub decides how a file is shown (`viewer`): HTML and SVG
 * never run with the hub's origin, text of any kind comes as text/plain. Pure helpers, shared by every surface.
 */

export type RunOutput = components["schemas"]["RunOutput"];
export type OutputSource = RunOutput["source"];
export type OutputViewer = RunOutput["viewer"];

/** Where a file the strips and the viewer show came from: a run's output's source, or a person's attachment. */
export type FileSource = OutputSource | "attached";

/**
 * What a strip, its chips and the viewer show: a run's output, or a file a person attached to a chat message
 * (`chatFileOutput` in lib/chats.ts), which has no run, no tool call and the source `attached`.
 */
export type ShownFile = Omit<RunOutput, "run_id" | "source" | "tool_call_id"> & { source: FileSource };

/** `outputs.SOURCES` of the API, in its order: sent on purpose, taken out of a tool's result, left in .evo-run/outputs/. */
export const OUTPUT_SOURCES = ["sent", "tool", "outputs"] as const satisfies readonly OutputSource[];

/** The scheme of the ACP `resource_link` an event names an output with: `evo-output:sha256:<hex>`. */
const LINK = /^evo-output:sha256:([0-9a-f]{64})$/;

/** The URL of an output's bytes as an attachment, which the browser saves under the output's name. */
export function downloadHref(url: string): string {
  return `${url}${url.includes("?") ? "&" : "?"}download=true`;
}

/** The last part of an output's name: a file of `.evo-run/outputs/` keeps its path below it as its name. */
export function baseName(name: string): string {
  return name.slice(name.lastIndexOf("/") + 1) || name;
}

/** The extension of a file's name, lower case, without the dot; "" when it has none. */
export function extensionOf(name: string): string {
  const base = baseName(name);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

export type OutputLink = { sha256: string; name: string | null; mediaType: string | null; size: number | null };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The outputs an event's ACP content links to (`{"type": "resource_link", "uri": "evo-output:sha256:<hex>", ...}`, as
 * `outputs.output_link` of the API writes it in place of an image of a tool's result), in order, each once. The link
 * may sit in a tool call content (`{"type": "content", "content": {...}}`) or in a list of them.
 */
export function outputLinks(content: unknown, found: OutputLink[] = [], depth = 0): OutputLink[] {
  if (depth > 4) return found;
  if (Array.isArray(content)) {
    for (const item of content) outputLinks(item, found, depth + 1);
    return found;
  }
  if (!isRecord(content)) return found;
  if (content.type === "resource_link" && typeof content.uri === "string") {
    const match = LINK.exec(content.uri);
    if (match && !found.some((link) => link.sha256 === match[1])) {
      found.push({
        sha256: match[1],
        name: typeof content.name === "string" ? content.name : null,
        mediaType: typeof content.mimeType === "string" ? content.mimeType : null,
        size: typeof content.size === "number" ? content.size : null,
      });
    }
    return found;
  }
  if ("content" in content) outputLinks(content.content, found, depth + 1);
  return found;
}

export type TextKind = "markdown" | "json" | "csv" | "code" | "plain";

const CODE = new Set([
  "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "sh", "bash", "zsh", "go", "rs", "rb", "java", "kt", "swift", "c", "h",
  "cpp", "cs", "php", "sql", "yaml", "yml", "toml", "xml", "html", "htm", "css", "scss", "ini", "conf", "dockerfile",
  "tf", "lua", "r", "dart", "vue", "svelte", "diff", "patch",
]);

/** How the viewer draws a text output: Markdown, JSON (pretty), CSV or TSV (a table), code (mono), or plain text. */
export function textKind(output: Pick<RunOutput, "name" | "media_type">): TextKind {
  const ext = extensionOf(output.name);
  const type = output.media_type;
  if (type === "text/markdown" || ext === "md" || ext === "markdown") return "markdown";
  if (type === "application/json" || type === "application/x-ndjson" || ext === "json" || ext === "jsonl" || ext === "ndjson") return "json";
  if (type === "text/csv" || type === "text/tab-separated-values" || ext === "csv" || ext === "tsv") return "csv";
  if (CODE.has(ext) || type.startsWith("text/x-") || type === "application/javascript" || type === "application/x-sh") return "code";
  return "plain";
}

export type SizeUnit = "byte" | "kilobyte" | "megabyte" | "gigabyte";

const UNITS: readonly SizeUnit[] = ["byte", "kilobyte", "megabyte", "gigabyte"];

/** A byte count in the unit a person reads best, in binary steps as file managers show them. */
export function byteParts(bytes: number): { value: number; unit: SizeUnit } {
  let step = 0;
  while (step < UNITS.length - 1 && bytes >= 1024 ** (step + 1)) step += 1;
  return { value: bytes / 1024 ** step, unit: UNITS[step] };
}
