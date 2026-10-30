import { linkedFiles } from "@/lib/attachments";
import type { GroupMessage } from "@/lib/chats";
import { docKey, type DocVersion, isDoc, versionKey } from "@/lib/doc-versions";
import type { ViewedFile } from "@/lib/outputs";

import type { Feed } from "./feed-model";

/**
 * The text documents of a chat's page (lib/doc-versions.ts): a file a person attached or an agent's run sent, named as
 * an earlier one, is that document's next version. The page's feed holds the latest messages and what the stream
 * brings; the hub's list of versions (`chatDocVersionsQuery`) holds the older ones too, and the panel merges both.
 */

/** The text documents a message carries: those its person attached, then those of its run. */
export function messageDocs(message: GroupMessage): ViewedFile[] {
  return [...linkedFiles(message.attachments, message.created_at), ...(message.outputs ?? [])].filter(isDoc);
}

/** Every version of the document `name` the page holds (threads included), oldest first, each once. */
export function docVersions(feed: Feed, name: string): DocVersion[] {
  const key = docKey(name);
  const seen = new Set<string>();
  return [...feed.entries.values()]
    .map((entry) => entry.message)
    .sort((a, b) => a.seq - b.seq)
    .flatMap((message) =>
      messageDocs(message)
        .filter((file) => docKey(file.name) === key)
        .map((file): DocVersion => ({ key: versionKey(file), file, author: message.author, at: message.created_at, seq: message.seq })),
    )
    .filter((version) => !seen.has(version.key) && Boolean(seen.add(version.key)));
}
