import { browserApi } from "./api/browser";
import { csrfHeaders } from "./api/csrf";
import { fromResponse, networkError } from "./api/errors";
import type { Attachment } from "./attachments";

/**
 * The upload of one file to the hub, from the browser: `POST {base}/attachments?name=NAME` with the file's bytes as the
 * body and its media type as Content-Type, on the hub's own origin (/v1, so the CSP's connect-src 'self' holds and
 * nothing is presigned). XMLHttpRequest, not fetch, because only it reports how much of the body went up. `base` is a
 * run's routes (`/v1/projects/{p}/runs/{id}`) or a chat's (`/v1/chats/{id}`).
 */
export async function uploadAttachment(
  base: string,
  file: File,
  onProgress: (loaded: number) => void,
  signal: AbortSignal,
): Promise<Attachment> {
  const headers = await csrfHeaders(browserApi());
  return new Promise<Attachment>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${base}/attachments?name=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("content-type", file.type || "application/octet-stream");
    request.setRequestHeader("accept", "application/json");
    for (const [name, value] of Object.entries(headers)) request.setRequestHeader(name, value);
    request.responseType = "text";
    request.upload.onprogress = (event) => onProgress(event.loaded);
    request.onload = () => {
      let body: unknown = null;
      try {
        body = request.responseText ? JSON.parse(request.responseText) : null;
      } catch {
        body = null;
      }
      if (request.status >= 200 && request.status < 300) return resolve(body as Attachment);
      const response = new Response(null, { status: request.status, headers: { "x-request-id": request.getResponseHeader("x-request-id") ?? "" } });
      reject(fromResponse(response, body));
    };
    request.onerror = () => reject(networkError(new Error("The upload did not reach the hub.")));
    request.onabort = () => reject(new DOMException("The upload was cancelled.", "AbortError"));
    if (signal.aborted) return reject(new DOMException("The upload was cancelled.", "AbortError"));
    signal.addEventListener("abort", () => request.abort(), { once: true });
    request.send(file);
  });
}

/** Deletes a file uploaded and not sent yet (`DELETE {base}/attachments/{id}`); a file already sent stays. */
export async function deleteAttachment(base: string, id: number): Promise<void> {
  const headers = await csrfHeaders(browserApi());
  let response: Response;
  try {
    response = await fetch(`${base}/attachments/${id}`, { method: "DELETE", headers, credentials: "same-origin" });
  } catch (cause) {
    throw networkError(cause);
  }
  if (!response.ok && response.status !== 404) throw fromResponse(response, await response.json().catch(() => null));
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
