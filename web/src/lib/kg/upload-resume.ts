/**
 * The uploads this browser started and did not finish, kept in localStorage so a reload, a closed tab or a lost
 * network does not lose them: the page lists them, and the same file chosen again resumes without hashing it again
 * (its name, size and last change match) and sends only the parts the store lacks. A record holds no byte of the file
 * and no path inside it: the archive's name, its size, its hash and what the upload was started with.
 */

const KEY = "evo.kg-uploads.v1";
/** A record older than this is dropped: the hub fails an upload left untouched for a day. */
const KEEP_MS = 24 * 60 * 60 * 1000;

export type PendingUpload = {
  project: string;
  id: number;
  name: string;
  size: number;
  lastModified: number;
  sha256: string;
  source: string;
  label: string | null;
  startedAt: number;
};

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null; // storage turned off: uploads still work, they just do not survive a reload
  }
}

function valid(value: unknown): value is PendingUpload {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.project === "string" &&
    typeof v.id === "number" &&
    typeof v.name === "string" &&
    typeof v.size === "number" &&
    typeof v.lastModified === "number" &&
    typeof v.sha256 === "string" &&
    typeof v.source === "string" &&
    (v.label === null || typeof v.label === "string") &&
    typeof v.startedAt === "number"
  );
}

function readAll(now = Date.now()): PendingUpload[] {
  const raw = storage()?.getItem(KEY);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(valid).filter((record) => now - record.startedAt < KEEP_MS) : [];
  } catch {
    return [];
  }
}

/** Told within this tab when the records change; other tabs hear the `storage` event. */
const CHANGED = "evo:kg-uploads";

function writeAll(records: PendingUpload[]): void {
  try {
    storage()?.setItem(KEY, JSON.stringify(records));
  } catch {
    // full or refused: nothing to keep
  }
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CHANGED));
}

/** For `useSyncExternalStore`: calls `onChange` when this tab or another changes the records. */
export function subscribePending(onChange: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === KEY || event.key === null) onChange();
  };
  window.addEventListener(CHANGED, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGED, onChange);
    window.removeEventListener("storage", onStorage);
  };
}

/** The records as stored, a string that stays the same while they do (a stable snapshot); "" on the server. */
export function pendingSnapshot(): string {
  return storage()?.getItem(KEY) ?? "";
}

/** The unfinished uploads of `project`, newest first. */
export function pendingUploads(project: string): PendingUpload[] {
  return readAll()
    .filter((record) => record.project === project)
    .sort((a, b) => b.startedAt - a.startedAt);
}

export function rememberUpload(record: PendingUpload): void {
  writeAll([record, ...readAll().filter((r) => !(r.project === record.project && r.id === record.id))]);
}

export function forgetUpload(project: string, id: number): void {
  writeAll(readAll().filter((r) => !(r.project === project && r.id === id)));
}

/** The record `file` resumes: same name, size and last change, in `project`. */
export function pendingFor(project: string, file: Pick<File, "name" | "size" | "lastModified">): PendingUpload | null {
  return (
    pendingUploads(project).find(
      (r) => r.name === file.name && r.size === file.size && r.lastModified === file.lastModified,
    ) ?? null
  );
}
