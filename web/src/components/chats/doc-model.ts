import { createContext } from "react";

import { linkedFiles } from "@/lib/attachments";
import type { GroupMessage } from "@/lib/chats";
import { baseName, textKind, type ViewedFile } from "@/lib/outputs";

import type { Feed } from "./feed-model";

/**
 * The Markdown files of a chat as versions of documents: a file a person attached or an agent's run sent, named as an
 * earlier one, is that document's next version (an agent that fixes a document sends it back under the same name). The
 * document panel (`doc-panel.tsx`) reads one beside the chat and shows what each new version changed.
 */
export type DocVersion = { key: string; file: ViewedFile; message: GroupMessage };

/** The most bytes the panel reads of one version; a bigger file opens from the message's files instead. */
export const MAX_DOC_BYTES = 2 * 1024 * 1024;

export function isDoc(file: Pick<ViewedFile, "name" | "media_type">): boolean {
  return textKind(file) === "markdown";
}

/** The key a document's versions share: the last part of its name, without case (an output keeps its path in it). */
export function docKey(name: string): string {
  return baseName(name.trim()).toLowerCase();
}

/** A version's own key: an attachment's id and an output's id are counted apart, so each is named by its kind. */
function versionKey(file: ViewedFile): string {
  return `${file.source === "attached" ? "attachment" : "output"}-${file.id}`;
}

/** The Markdown files a message carries: those its person attached, then those of its run. */
export function messageDocs(message: GroupMessage): ViewedFile[] {
  return [...linkedFiles(message.attachments, message.created_at), ...(message.outputs ?? [])].filter(isDoc);
}

/** Every version of the document `name` the page holds (threads included), oldest first. */
export function docVersions(feed: Feed, name: string): DocVersion[] {
  const key = docKey(name);
  return [...feed.entries.values()]
    .map((entry) => entry.message)
    .sort((a, b) => a.seq - b.seq)
    .flatMap((message) =>
      messageDocs(message)
        .filter((file) => docKey(file.name) === key)
        .map((file) => ({ key: versionKey(file), file, message })),
    );
}

/** Opens a document beside the chat by its name; null where the page has no panel (a message drawn elsewhere). */
export const OpenDocContext = createContext<((name: string) => void) | null>(null);
