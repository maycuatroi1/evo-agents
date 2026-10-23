import type { components } from "./api/schema";
import type { OutputViewer, ViewedFile } from "./outputs";

/**
 * The files people attach to their messages (docs/hub.md, Attachments): to a run's agent (a held run, an author run's
 * chat) and in a chat (group, direct, a thread). A file is uploaded on its own as it is picked, dropped or pasted,
 * then the message names the files it carries by id. The limits are the hub's: at most 50 MiB a file and 10 files a
 * message; the web refuses past them before it uploads anything. A file sent shows as a run's outputs do, in the same
 * strips and viewer: `viewerOf` ports the hub's rule (`outputs.viewer`) so the web opens it the same way. Pure helpers,
 * also read by next.config.ts (the body limit of the /v1 rewrite).
 */

export type Attachment = components["schemas"]["Attachment"];
export type SentFile = components["schemas"]["SentFile"];
export type LinkedAttachment = components["schemas"]["LinkedAttachment"];

/** The most bytes one file may have: `attachments.MAX_BYTES` of the API. */
export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
/** The most files one message may carry: `attachments.MAX_PER_MESSAGE` of the API. */
export const MAX_ATTACHMENTS = 10;

const IMAGE = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/svg+xml"]);
const VIDEO = new Set(["video/mp4", "video/webm", "video/quicktime", "video/ogg"]);
const AUDIO = new Set(["audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/mp4", "audio/aac", "audio/flac"]);
const TEXT = new Set([
  "application/json", "application/x-ndjson", "application/yaml", "application/x-yaml", "application/toml",
  "application/xml", "application/javascript", "application/x-sh", "application/sql",
]);

/** How the web shows a file of this media type, by the hub's rule for outputs (`outputs.viewer`). */
export function viewerOf(mediaType: string): OutputViewer {
  const type = mediaType.split(";")[0].trim().toLowerCase();
  if (IMAGE.has(type)) return "image";
  if (VIDEO.has(type)) return "video";
  if (AUDIO.has(type)) return "audio";
  if (type === "application/pdf") return "pdf";
  if (type === "text/html" || type === "application/xhtml+xml") return "html";
  if (type.startsWith("text/") || TEXT.has(type)) return "text";
  return "none";
}

/** The URL of the bytes of a file sent to a run's agent, for a reader of the run whose inbox took it. */
export function runAttachmentUrl(project: string, runId: number, id: number): string {
  return `/v1/projects/${encodeURIComponent(project)}/runs/${runId}/attachments/${id}/content`;
}

/** The base of the attachment routes a composer uploads to: a run's (`.../runs/{id}`) or a chat's (`/v1/chats/{id}`). */
export function runUploadBase(project: string, runId: number): string {
  return `/v1/projects/${encodeURIComponent(project)}/runs/${runId}`;
}

export function chatUploadBase(chatId: number): string {
  return `/v1/chats/${chatId}`;
}

type Named = { id: number; name: string; media_type: string; size: number; sha256?: string };

/** A file a message carries, as the strips and the viewer show it: `url` where its bytes are, `at` when it was sent. */
export function attachedFile(file: Named, url: string, at: string): ViewedFile {
  return {
    id: file.id,
    source: "attached",
    name: file.name,
    media_type: file.media_type,
    viewer: viewerOf(file.media_type),
    size: file.size,
    sha256: file.sha256 ?? "",
    caption: null,
    created_at: at,
    url,
  };
}

/** The files of a chat's message, which the hub lists with their URL. */
export function linkedFiles(files: readonly LinkedAttachment[] | null | undefined, at: string): ViewedFile[] {
  return (files ?? []).map((file) => attachedFile(file, file.url, at));
}

/** The files of a message to a run's agent, read from the run `runId` whose inbox took it. */
export function runFiles(files: readonly SentFile[] | null | undefined, project: string, runId: number, at: string): ViewedFile[] {
  return (files ?? []).map((file) => attachedFile(file, runAttachmentUrl(project, runId, file.id), at));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The files a `user_message` event's body lists (`attachments`), each read defensively; [] when it lists none. */
export function eventFiles(value: unknown): SentFile[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) =>
    isRecord(entry) && typeof entry.id === "number" && typeof entry.name === "string"
      ? [{ id: entry.id, name: entry.name, media_type: typeof entry.media_type === "string" ? entry.media_type : "application/octet-stream", size: typeof entry.size === "number" ? entry.size : 0 }]
      : [],
  );
}

/** What the web refuses before an upload: a file over the limit, or more files than a message carries. */
export type Refusal = { kind: "big"; names: string[] } | { kind: "many"; names: string[] };

/**
 * Which of `files` a message that already carries `count` files may take, in order, and what it refuses: a file over
 * 50 MiB, then the files past the tenth.
 */
export function admit(files: readonly File[], count: number): { taken: File[]; refused: Refusal[] } {
  const big = files.filter((file) => file.size > MAX_ATTACHMENT_BYTES);
  const fitting = files.filter((file) => file.size <= MAX_ATTACHMENT_BYTES);
  const room = Math.max(MAX_ATTACHMENTS - count, 0);
  const taken = fitting.slice(0, room);
  const over = fitting.slice(room);
  const refused: Refusal[] = [];
  if (big.length) refused.push({ kind: "big", names: big.map((file) => file.name) });
  if (over.length) refused.push({ kind: "many", names: over.map((file) => file.name) });
  return { taken, refused };
}
