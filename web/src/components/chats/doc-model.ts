import { createContext } from "react";

import { chatFileOutput, type GroupMessage } from "@/lib/chats";
import { type ShownFile, textKind } from "@/lib/outputs";

import type { Feed } from "./feed-model";

/**
 * The Markdown files of a chat as versions of documents: a file a person attached or an agent's run sent, named as an
 * earlier one, is that document's next version (an agent that fixes a document sends it back under the same name). The
 * document panel (`doc-panel.tsx`) reads one beside the chat and shows what each new version changed.
 */
export type DocVersion = { file: ShownFile; message: GroupMessage };

export function isDoc(file: Pick<ShownFile, "name" | "media_type">): boolean {
  return textKind(file) === "markdown";
}

/** The key a document's versions share: its name, without case. */
export function docKey(name: string): string {
  return name.trim().toLowerCase();
}

/** The Markdown files a message carries: those its person attached, then those of its run. */
export function messageDocs(message: GroupMessage): ShownFile[] {
  return [
    ...(message.files ?? []).map(chatFileOutput),
    ...(message.outputs ?? []),
  ].filter(isDoc);
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
        .map((file) => ({ file, message })),
    );
}

/** Opens a document beside the chat by its name; null where the page has no panel (a message drawn elsewhere). */
export const OpenDocContext = createContext<((name: string) => void) | null>(
  null,
);
