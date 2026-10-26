/**
 * The rules of the Docs pages that need no React: the hub's limits, which version a page shows, how an image's source
 * in the Markdown finds the image a version holds, and the names the editor gives the images it uploads. The hub
 * checks the same (evo_agents.hub.tables_docs); the web repeats them so nothing goes out that it would refuse.
 */

/** A document's slug (evo_agents.hub.tables_docs.SLUG). */
export const SLUG = /^[a-z0-9][a-z0-9-]{0,99}$/;
/** The name a version gives an image (evo_agents.hub.tables_docs.IMAGE_NAME). */
export const IMAGE_NAME = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(\/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,4}$/;
export const MAX_TITLE = 300;
export const MAX_BODY = 500_000;
export const MAX_SUMMARY = 500;
export const MAX_IMAGES = 100;
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const;

export type Viewer = { login: string; role: "reader" | "writer" | "admin" | null };

export function canWrite(viewer: Viewer | null): boolean {
  return viewer?.role === "writer" || viewer?.role === "admin";
}

/** The version a page asks for in `v`: a whole number from 1, else null for the latest. */
export function versionParam(value: string | string[] | undefined): number | null {
  const text = Array.isArray(value) ? value[0] : value;
  if (!text || !/^[1-9][0-9]{0,8}$/.test(text)) return null;
  return Number(text);
}

/** An image's source as a version names it: `./` dropped, percent-escapes decoded; null for a URL, a path from the
 * root, an anchor or a source that does not decode. */
export function imageKey(src: string): string | null {
  if (!src || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(src) || src.startsWith("/") || src.startsWith("#")) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(src);
  } catch {
    return null;
  }
  return decoded
    .split("/")
    .filter((part) => part !== "." && part !== "")
    .join("/");
}

/** The URL of each image `images` names, by its name; the resolver SafeMarkdown takes. */
export function imageResolver(images: readonly { name: string; url: string }[]): (src: string) => string | null {
  const urls = new Map(images.map((image) => [image.name, image.url]));
  return (src) => {
    const key = imageKey(src);
    return key === null ? null : (urls.get(key) ?? null);
  };
}

const IMAGE_SOURCE = /!\[[^\]]*\]\(\s*(<[^>\n]*>|[^\s)]+)/g;

/** The names of `known` that the Markdown shows as an image, in the order it first shows them. */
export function shownImages(body: string, known: ReadonlySet<string>): string[] {
  const found: string[] = [];
  for (const match of body.matchAll(IMAGE_SOURCE)) {
    const raw = match[1].startsWith("<") ? match[1].slice(1, -1) : match[1];
    const key = imageKey(raw);
    if (key !== null && known.has(key) && !found.includes(key)) found.push(key);
  }
  return found;
}

/** The name the editor gives an uploaded file: `images/<its name, cleaned>`, made unique among `taken`. */
export function uploadName(fileName: string, taken: ReadonlySet<string>): string {
  const cleaned = fileName.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+/, "").slice(0, 100) || "image";
  const dot = cleaned.lastIndexOf(".");
  const [stem, extension] = dot > 0 ? [cleaned.slice(0, dot), cleaned.slice(dot)] : [cleaned, ""];
  let name = `images/${cleaned}`;
  for (let number = 2; taken.has(name); number += 1) name = `images/${stem}-${number}${extension}`;
  return name;
}

/** A slug made from a title: lower-case letters and digits, dashes between; "" when nothing is left. */
export function slugOf(title: string): string {
  return title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 100)
    .replace(/-+$/, "");
}

/** The Markdown that shows image `name`, with the file's name as its text. */
export function imageMarkdown(name: string, alt: string): string {
  return `![${alt.replace(/[[\]\n]/g, " ").trim() || name}](${name})`;
}
