import "server-only";

/**
 * Reads a run's diff from the blob store on the web's server, through the presigned GET the API signs. The browser
 * never fetches the store itself, so the bucket needs no CORS rule and the page's connect-src stays 'self'. The read is
 * bounded: the API keeps a diff of at most 8 MiB, and the page stops reading one byte past that.
 */

/** `KIND_LIMITS["run-diff"]` of the API. */
export const MAX_DIFF_BYTES = 8 * 1024 * 1024;
const READ_TIMEOUT_MS = 20_000;

export type DiffBlob = { status: "ok"; text: string; bytes: number; cut: boolean } | { status: "failed"; reason: string };

export async function readDiffBlob(url: string, fetcher: typeof fetch = fetch): Promise<DiffBlob> {
  let response: Response;
  try {
    response = await fetcher(url, { cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MS) });
  } catch (error) {
    return { status: "failed", reason: error instanceof Error ? error.message : String(error) };
  }
  if (!response.ok || !response.body) {
    return { status: "failed", reason: `the blob store answered HTTP ${response.status}` };
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  let cut = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const room = MAX_DIFF_BYTES - bytes;
      if (value.byteLength > room) {
        chunks.push(value.subarray(0, room));
        bytes += room;
        cut = true;
        await reader.cancel();
        break;
      }
      chunks.push(value);
      bytes += value.byteLength;
    }
  } catch (error) {
    return { status: "failed", reason: error instanceof Error ? error.message : String(error) };
  }
  const all = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { status: "ok", text: new TextDecoder("utf-8", { fatal: false }).decode(all), bytes, cut };
}
