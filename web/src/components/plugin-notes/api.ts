import { queryOptions } from "@tanstack/react-query";

import type { ApiClient } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { fromResponse, networkError } from "@/lib/api/errors";

/**
 * The routes of the core plugin notes (tests/hub/core_notes.py): GET and POST /v1/x/notes/{project}/entries. A core
 * plugin's routes are in the hub's OpenAPI document when the hub's image carries the plugin; this one belongs to the
 * tests, so its two routes are typed here by hand.
 */
export type Note = { body: string; author: string };
export const MAX_NOTE_CHARS = 200; // NoteIn.body

function entriesUrl(project: string): string {
  return `${window.location.origin}/v1/x/notes/${encodeURIComponent(project)}/entries`;
}

async function json<T>(pending: Promise<Response>): Promise<T> {
  let response: Response;
  try {
    response = await pending;
  } catch (cause) {
    throw networkError(cause);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw fromResponse(response, body);
  return body as T;
}

export const notesKey = (project: string) => ["projects", project, "plugins", "x", "notes", "entries"] as const;

export const notesQuery = (project: string) =>
  queryOptions({
    queryKey: notesKey(project),
    queryFn: ({ signal }) => json<Note[]>(fetch(entriesUrl(project), { signal, credentials: "same-origin" })),
  });

export async function addNote(api: ApiClient, project: string, body: string): Promise<Note> {
  const headers = { "content-type": "application/json", ...(await csrfHeaders(api)) };
  return json<Note>(fetch(entriesUrl(project), { method: "POST", headers, body: JSON.stringify({ body }), credentials: "same-origin" }));
}
