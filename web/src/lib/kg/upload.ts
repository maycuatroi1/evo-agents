import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { ApiError, fromResponse, networkError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

/**
 * An archive sent from the browser as a source of a project's graph (plan kg-upload-archive): the same three calls as
 * `evo-agents hub kg upload`, POST /v1/kg/{project}/uploads (start, or resume the upload in flight of the same
 * archive), then each part the store lacks, then POST .../complete. The parts go to the hub's own origin (PUT
 * .../parts/{n}), which relays them to the blob store, since the page's connect-src is 'self'. Each part is retried a
 * few times; a part that still fails stops the upload, and starting it again sends only what the store lacks.
 */

type Schemas = components["schemas"];
export type KgUpload = Schemas["KgUpload"];
export type KgUploadStarted = Schemas["KgUploadStarted"];

/** The archives the hub reads, as `kg_uploads.SUFFIXES` lists them. */
export const ARCHIVE_SUFFIXES = [".zip", ".tar", ".tar.gz", ".tgz"] as const;
export const ARCHIVE_ACCEPT = ".zip,.tar,.tar.gz,.tgz,application/zip,application/x-tar,application/gzip";
/** Parts sent at once, and tries of each before the upload stops. */
export const PARALLEL_PARTS = 3;
export const PART_TRIES = 3;

export function isArchive(name: string): boolean {
  const lower = name.toLowerCase();
  return ARCHIVE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

/** The source an archive becomes without one given: `kg_uploads.default_source`. */
export function defaultSource(name: string): string {
  let stem = name;
  for (const suffix of [".tar.gz", ".tgz", ".zip", ".tar"]) {
    if (stem.toLowerCase().endsWith(suffix)) {
      stem = stem.slice(0, -suffix.length);
      break;
    }
  }
  const cleaned = stem
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "")
    .slice(0, 200);
  return cleaned || "archive";
}

export type StartAsk = {
  project: string;
  name: string;
  size: number;
  sha256: string;
  source?: string | null;
  label?: string | null;
  newProject?: boolean;
};

export async function startUpload(ask: StartAsk, headers: Record<string, string>): Promise<KgUploadStarted> {
  return call(
    browserApi().POST("/v1/kg/{project}/uploads", {
      params: { path: { project: ask.project } },
      headers,
      body: {
        name: ask.name,
        size: ask.size,
        sha256: ask.sha256,
        source: ask.source || null,
        label: ask.label || null,
        new_project: Boolean(ask.newProject),
      },
    }),
  );
}

export async function completeUpload(project: string, id: number, headers: Record<string, string>): Promise<KgUpload> {
  return call(
    browserApi().POST("/v1/kg/{project}/uploads/{upload_id}/complete", {
      params: { path: { project, upload_id: id } },
      headers,
    }),
  );
}

/** PUT one part through the hub. XMLHttpRequest, not fetch, since only it reports the bytes sent as they go. */
export function sendPart(
  url: string,
  body: Blob,
  headers: Record<string, string>,
  onProgress: (sent: number) => void,
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("The upload was cancelled.", "AbortError"));
    const request = new XMLHttpRequest();
    request.open("PUT", url);
    request.setRequestHeader("content-type", "application/octet-stream");
    request.setRequestHeader("accept", "application/json");
    for (const [name, value] of Object.entries(headers)) request.setRequestHeader(name, value);
    request.upload.onprogress = (event) => onProgress(event.loaded);
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) return resolve();
      let parsed: unknown = null;
      try {
        parsed = request.responseText ? JSON.parse(request.responseText) : null;
      } catch {
        parsed = null;
      }
      const response = new Response(null, {
        status: request.status,
        headers: { "x-request-id": request.getResponseHeader("x-request-id") ?? "" },
      });
      reject(fromResponse(response, parsed));
    };
    request.onerror = () => reject(networkError(new Error("The part did not reach the hub.")));
    request.onabort = () => reject(new DOMException("The upload was cancelled.", "AbortError"));
    signal.addEventListener("abort", () => request.abort(), { once: true });
    request.send(body);
  });
}

export function partUrl(project: string, id: number, number: number): string {
  return `/v1/kg/${encodeURIComponent(project)}/uploads/${id}/parts/${number}`;
}

/** {number: [start, end)} of each part of an archive of `size` bytes cut every `partSize`, numbered from 1. */
export function partRanges(size: number, partSize: number): Map<number, [number, number]> {
  const ranges = new Map<number, [number, number]>();
  for (let start = 0, number = 1; start < size; start += partSize, number++) {
    ranges.set(number, [start, Math.min(start + partSize, size)]);
  }
  return ranges;
}

/** A failure worth a second try: no answer, the store or the hub busy. A refusal (4xx) is not. */
function retryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.status === 0 || error.status === 429 || error.status >= 500;
}

export type PartsProgress = (number: number, sent: number) => void;

/**
 * Send the parts of `file` the store lacks (`started.done` it holds), PARALLEL_PARTS at a time, each tried up to
 * PART_TRIES times; `onPart` gets each part's bytes sent so far, its size once the hub took it.
 */
export async function sendMissingParts(
  project: string,
  file: Blob,
  started: KgUploadStarted,
  headers: Record<string, string>,
  onPart: PartsProgress,
  signal: AbortSignal,
  wait: (ms: number) => Promise<void> = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
): Promise<void> {
  const ranges = partRanges(started.size, started.part_size);
  const held = new Set(started.done);
  const queue = [...ranges.keys()].filter((number) => !held.has(number));
  const worker = async () => {
    for (let number = queue.shift(); number !== undefined; number = queue.shift()) {
      const [start, end] = ranges.get(number)!;
      for (let attempt = 1; ; attempt++) {
        try {
          await sendPart(
            partUrl(project, started.id, number),
            file.slice(start, end),
            headers,
            (sent) => onPart(number, Math.min(sent, end - start)),
            signal,
          );
          onPart(number, end - start);
          break;
        } catch (error) {
          onPart(number, 0);
          if (attempt >= PART_TRIES || !retryable(error) || signal.aborted) throw error;
          await wait(1000 * 2 ** (attempt - 1));
        }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL_PARTS, queue.length) }, worker));
}

/** The session's CSRF header, read once per upload: every write of it carries it. */
export function uploadHeaders(): Promise<Record<string, string>> {
  return csrfHeaders(browserApi());
}
