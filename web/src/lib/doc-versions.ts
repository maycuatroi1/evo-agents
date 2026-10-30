import { queryOptions } from "@tanstack/react-query";

import { call } from "./api/client";
import type { components } from "./api/schema";
import type { ChatAuthor } from "./chats";
import { baseName, textKind, type ViewedFile } from "./outputs";
import type { ApiSource } from "./queries";

/**
 * The text files of a chat or a run as versions of documents (docs/hub.md, Versions of a document): a file a person
 * attached or a run sent, named as an earlier one, is that document's next version (an agent that fixes a document
 * sends it back under the same name). The hub lists every version a reader may read (`GET .../doc-versions?name=`),
 * so a document opened after a reload has them all even when the page holds only the latest messages; what a chat's
 * stream brings after that is added from the page (`mergeVersions`). The document panel
 * (`components/data/doc-panel.tsx`) reads one and shows what each version changed.
 */

export type HubDocVersion = components["schemas"]["DocVersion"];

/** One version: the file, who sent it (null for a run's own output), when, and the chat event that carried it. */
export type DocVersion = { key: string; file: ViewedFile; author: ChatAuthor | null; at: string; seq: number | null };

/** The most bytes the panel reads of one version; a bigger file opens from the message's files instead. */
export const MAX_DOC_BYTES = 2 * 1024 * 1024;

/**
 * Whether the panel opens the file: text the hub shows as text (Markdown, plain text, JSON, CSV, YAML, code), or a
 * file the hub keeps as bytes whose name says it is one of those. Images, PDFs, archives and HTML are not documents.
 */
export function isDoc(file: Pick<ViewedFile, "name" | "media_type" | "viewer">): boolean {
  if (file.viewer === "text") return true;
  return file.viewer === "none" && textKind(file) !== "plain";
}

/** The key a document's versions share: the last part of its name, without case (an output keeps its path in it). */
export function docKey(name: string): string {
  return baseName(name.trim()).toLowerCase();
}

/** A version's own key: an attachment's id and an output's id are counted apart, so each is named by its kind. */
export function versionKey(file: Pick<ViewedFile, "source" | "id">): string {
  return `${file.source === "attached" ? "attachment" : "output"}-${file.id}`;
}

/** A version as the hub lists it. */
export function hubVersion(version: HubDocVersion): DocVersion {
  const file: ViewedFile = {
    id: version.id,
    source: version.source,
    name: version.name,
    media_type: version.media_type,
    viewer: version.viewer,
    size: version.size,
    sha256: version.sha256,
    caption: version.caption,
    created_at: version.created_at,
    url: version.url,
  };
  return { key: versionKey(file), file, author: version.author, at: version.created_at, seq: version.seq };
}

function order(a: DocVersion, b: DocVersion): number {
  if (a.seq !== null && b.seq !== null) return a.seq - b.seq;
  return Date.parse(a.at) - Date.parse(b.at);
}

/**
 * The versions of several lists as one, oldest first, each once (the first list's copy kept), documents only: a chat's
 * by the event that carried them (a message's attachments before its outputs, as each list has them), a run's by time.
 */
export function mergeVersions(...lists: readonly (readonly DocVersion[])[]): DocVersion[] {
  const byKey = new Map<string, DocVersion>();
  for (const list of lists) for (const version of list) if (!byKey.has(version.key) && isDoc(version.file)) byKey.set(version.key, version);
  return [...byKey.values()].sort(order);
}

export const docVersionKeys = {
  chat: (chatId: number, name: string) => ["doc-versions", "chat", chatId, docKey(name)] as const,
  run: (project: string, runId: number, name: string) => ["doc-versions", "run", project, runId, docKey(name)] as const,
};

/** Every version of the document `name` in a chat (threads included) that the reader may read, oldest first. */
export const chatDocVersionsQuery = (api: ApiSource, chatId: number, name: string) =>
  queryOptions({
    queryKey: docVersionKeys.chat(chatId, name),
    queryFn: async ({ signal }) => {
      const answer = await call(api().GET("/v1/chats/{chat_id}/doc-versions", { params: { path: { chat_id: chatId }, query: { name } }, signal }));
      return answer.versions.map(hubVersion);
    },
    retry: 1,
  });

/** Every version of the document `name` in a run: the files of its members' messages and the run's outputs. */
export const runDocVersionsQuery = (api: ApiSource, project: string, runId: number, name: string, refetchInterval: number | false = false) =>
  queryOptions({
    queryKey: docVersionKeys.run(project, runId, name),
    queryFn: async ({ signal }) => {
      const answer = await call(
        api().GET("/v1/projects/{project}/runs/{run_id}/doc-versions", { params: { path: { project, run_id: runId }, query: { name } }, signal }),
      );
      return answer.versions.map(hubVersion);
    },
    refetchInterval,
    retry: 1,
  });
