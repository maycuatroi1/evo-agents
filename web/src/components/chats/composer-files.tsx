"use client";

import { File as FileIcon, LoaderCircle, TriangleAlert, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { useByteSize } from "@/components/data/byte-size";
import type { WriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/api/errors";
import { browserApi } from "@/lib/api/browser";
import { uploadChatFile } from "@/lib/chat-queries";
import { type ChatFile, MAX_FILE_BYTES, MAX_MESSAGE_FILES } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { useChatFailure } from "./hooks";

/** A file picked for the next message: uploading (neither `uploaded` nor `error`), uploaded, or refused. */
export type Attachment = { key: number; file: File; uploaded: ChatFile | null; error: WriteFailure | null };

/**
 * The files picked for the next message of a chat (web/DESIGN.md, Chats): each uploads at once
 * (`POST /v1/chats/{id}/files`), and the message names those uploaded in `file_ids`. A file over 25 MiB, an empty one,
 * and the files past ten are refused before they are sent, the reason in `refusal`; removing a file still uploading
 * stops its upload, and so does leaving the chat.
 */
export function useAttachments(chatId: number) {
  const t = useTranslations("chats.composer.files");
  const format = useFormatter();
  const size = useByteSize();
  const failure = useChatFailure();
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [refusal, setRefusal] = useState<string | null>(null);
  const nextKey = useRef(0);
  const uploads = useRef(new Map<number, AbortController>());

  useEffect(() => {
    const pending = uploads.current;
    return () => {
      for (const controller of pending.values()) controller.abort();
      pending.clear();
    };
  }, []);

  const update = (key: number, change: Partial<Attachment>) =>
    setAttachments((list) => list.map((attachment) => (attachment.key === key ? { ...attachment, ...change } : attachment)));

  const upload = (file: File) => {
    const key = nextKey.current++;
    const controller = new AbortController();
    uploads.current.set(key, controller);
    setAttachments((list) => [...list, { key, file, uploaded: null, error: null }]);
    uploadChatFile(browserApi(), chatId, file, controller.signal)
      .then(
        (uploaded) => update(key, { uploaded }),
        (cause: unknown) => {
          if (controller.signal.aborted) return;
          const error = failure(cause);
          update(key, { error: isApiError(cause) && cause.info.status === 413 ? { ...error, text: t("tooBigOne", { max: size(MAX_FILE_BYTES) }), detail: null } : error });
        },
      )
      .finally(() => uploads.current.delete(key));
  };

  /** Take the files picked, dropped or pasted: those that may go upload, and the rest are refused with the reason. */
  const add = (picked: readonly File[]) => {
    if (picked.length === 0) return;
    const names = (files: readonly File[]) => format.list(files.map((file) => file.name), { type: "conjunction" });
    const big = picked.filter((file) => file.size > MAX_FILE_BYTES);
    const empty = picked.filter((file) => file.size === 0);
    const fit = picked.filter((file) => file.size > 0 && file.size <= MAX_FILE_BYTES);
    const taken = fit.slice(0, Math.max(MAX_MESSAGE_FILES - attachments.length, 0));
    const left = fit.length - taken.length;
    const reasons = [
      big.length ? t("tooBig", { names: names(big), max: size(MAX_FILE_BYTES) }) : null,
      empty.length ? t("empty", { names: names(empty) }) : null,
      left ? t("tooMany", { max: MAX_MESSAGE_FILES, count: left }) : null,
    ].filter(Boolean);
    setRefusal(reasons.length ? reasons.join(" ") : null);
    taken.forEach(upload);
  };

  const remove = (key: number) => {
    uploads.current.get(key)?.abort();
    uploads.current.delete(key);
    setAttachments((list) => list.filter((attachment) => attachment.key !== key));
    setRefusal(null);
  };

  const clear = useCallback(() => {
    setAttachments([]);
    setRefusal(null);
  }, []);

  return {
    attachments,
    refusal,
    add,
    remove,
    clear,
    uploading: attachments.some((attachment) => !attachment.uploaded && !attachment.error),
    failed: attachments.some((attachment) => attachment.error !== null),
    fileIds: attachments.flatMap((attachment) => (attachment.uploaded ? [attachment.uploaded.id] : [])),
  };
}

/**
 * The files picked for the next message, as chips under the text: each with its name and size, a spinner while it
 * uploads, the reason in `danger` when it could not, and Remove.
 */
export function AttachmentList({ attachments, onRemove, testId }: { attachments: readonly Attachment[]; onRemove: (key: number) => void; testId: string }) {
  const t = useTranslations("chats.composer.files");
  const size = useByteSize();
  if (attachments.length === 0) return null;
  return (
    <ul aria-label={t("label")} className="flex flex-wrap gap-1.5 px-3 pb-2" data-testid={`${testId}-files`}>
      {attachments.map(({ key, file, uploaded, error }) => {
        const state = error ? "failed" : uploaded ? "uploaded" : "uploading";
        return (
          <li
            key={key}
            className={cn(
              "flex h-9 max-w-full min-w-0 items-center gap-1.5 rounded-sm border bg-card pr-0.5 pl-2 text-[13px] leading-[18px]",
              error && "border-danger-solid",
            )}
            title={error ? [error.text, error.detail].filter(Boolean).join(" ") : file.name}
            data-testid={`${testId}-file`}
            data-state={state}
          >
            {state === "uploading" ? (
              <LoaderCircle className="size-3.5 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none" aria-hidden="true" />
            ) : state === "failed" ? (
              <TriangleAlert className="size-3.5 shrink-0 text-danger" aria-hidden="true" />
            ) : (
              <FileIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            )}
            <span className="max-w-48 min-w-0 truncate font-medium text-foreground">{file.name}</span>
            {error ? (
              <span className="max-w-56 min-w-0 truncate text-danger" data-testid={`${testId}-file-error`}>
                {error.detail ?? error.text}
              </span>
            ) : (
              <span className="shrink-0 text-muted-foreground tabular-nums">{size(file.size)}</span>
            )}
            {state === "uploading" ? <span className="sr-only">{t("uploading")}</span> : null}
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={t("remove", { name: file.name })}
              title={t("remove", { name: file.name })}
              onClick={() => onRemove(key)}
              data-testid={`${testId}-file-remove`}
            >
              <X aria-hidden="true" />
            </Button>
          </li>
        );
      })}
    </ul>
  );
}
