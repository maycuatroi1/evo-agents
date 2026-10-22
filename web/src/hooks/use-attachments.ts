"use client";

import { useCallback, useRef, useState } from "react";

import { isApiError } from "@/lib/api/errors";
import { deleteAttachment, isAbort, uploadAttachment } from "@/lib/attachment-upload";
import { admit, type Refusal } from "@/lib/attachments";

/**
 * The files of a message being written, one upload each, shared by every composer (a held run's, an author run's
 * Chat tab, a chat's, a thread's). A file starts uploading once it is picked, dropped or pasted; its chip shows how far
 * it went, and removing it cancels the upload or deletes the file uploaded. `settled()` waits for every upload, then
 * gives the ids the message names, or throws `FilesNotReady` when one failed (its chip offers Retry). After a send,
 * `clear()` empties the list without deleting: the message carries the files now.
 */

export type Upload = {
  key: string;
  file: File;
  /** Bytes sent so far. */
  loaded: number;
  status: "uploading" | "done" | "failed";
  id: number | null;
  /** Why it failed: the hub refused it as too big, refused it otherwise (its words), or did not answer. */
  error: { kind: "big" | "refused" | "network"; message: string | null } | null;
};

export class FilesNotReady extends Error {
  constructor() {
    super("A file did not upload.");
    this.name = "FilesNotReady";
  }
}

export type Attachments = ReturnType<typeof useAttachments>;

export function useAttachments(base: string) {
  const list = useRef<Upload[]>([]);
  const running = useRef(new Map<string, { promise: Promise<void>; controller: AbortController }>());
  const counter = useRef(0);
  const [items, setItems] = useState<readonly Upload[]>([]);
  const [refusals, setRefusals] = useState<Refusal[]>([]);
  // The list as the latest change left it, read by the callbacks between renders; the state shows it.
  const commit = useCallback((next: Upload[]) => {
    list.current = next;
    setItems(next);
  }, []);

  const patch = useCallback(
    (key: string, change: Partial<Upload>) => commit(list.current.map((item) => (item.key === key ? { ...item, ...change } : item))),
    [commit],
  );

  const start = useCallback(
    (item: Upload) => {
      const controller = new AbortController();
      const promise = uploadAttachment(base, item.file, (loaded) => patch(item.key, { loaded }), controller.signal).then(
        (attachment) => patch(item.key, { status: "done", id: attachment.id, loaded: item.file.size, error: null }),
        (cause: unknown) => {
          if (isAbort(cause)) return;
          const status = isApiError(cause) ? cause.status : 0;
          const kind = status === 413 ? "big" : status === 0 ? "network" : "refused";
          patch(item.key, { status: "failed", error: { kind, message: isApiError(cause) ? cause.info.message : null } });
        },
      );
      running.current.set(item.key, { promise, controller });
    },
    [base, patch],
  );

  const add = useCallback(
    (files: readonly File[]) => {
      if (files.length === 0) return;
      const { taken, refused } = admit(files, list.current.length);
      setRefusals(refused);
      const added = taken.map((file): Upload => ({ key: `f${(counter.current += 1)}`, file, loaded: 0, status: "uploading", id: null, error: null }));
      commit([...list.current, ...added]);
      added.forEach(start);
    },
    [commit, start],
  );

  const remove = useCallback(
    (key: string) => {
      const item = list.current.find((entry) => entry.key === key);
      if (!item) return;
      running.current.get(key)?.controller.abort();
      running.current.delete(key);
      commit(list.current.filter((entry) => entry.key !== key));
      setRefusals([]);
      // A file the hub holds and no message names: deleted, so it does not wait out its expiry. A failure is left to it.
      if (item.id !== null) void deleteAttachment(base, item.id).catch(() => undefined);
    },
    [base, commit],
  );

  const retry = useCallback(
    (key: string) => {
      const item = list.current.find((entry) => entry.key === key);
      if (!item || item.status !== "failed") return;
      const again = { ...item, status: "uploading" as const, loaded: 0, error: null };
      commit(list.current.map((entry) => (entry.key === key ? again : entry)));
      start(again);
    },
    [commit, start],
  );

  /** Waits for every upload of the list as it is now, and those retried meanwhile; the ids of the files, in order. */
  const settled = useCallback(async (): Promise<number[]> => {
    while (list.current.some((item) => item.status === "uploading")) {
      await Promise.all(list.current.map((item) => running.current.get(item.key)?.promise));
    }
    if (list.current.some((item) => item.status !== "done")) throw new FilesNotReady();
    return list.current.map((item) => item.id!);
  }, []);

  const clear = useCallback(() => {
    running.current.clear();
    commit([]);
    setRefusals([]);
  }, [commit]);

  return {
    items,
    refusals,
    add,
    remove,
    retry,
    settled,
    clear,
    /** Some file is still going up. */
    busy: items.some((item) => item.status === "uploading"),
    count: items.length,
  };
}
