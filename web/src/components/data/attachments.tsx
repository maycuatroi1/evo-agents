"use client";

import { Paperclip, RotateCw, TriangleAlert, Upload as UploadIcon, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import {
  type ClipboardEvent,
  type ComponentProps,
  createContext,
  type DragEvent,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import type { Attachments, Upload } from "@/hooks/use-attachments";
import { MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, viewerOf } from "@/lib/attachments";
import { baseName } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { useByteSize } from "./byte-size";
import { OutputKindIcon } from "./output-tile";

/**
 * The parts every composer attaches files with (hooks/use-attachments.ts holds the uploads): the attach button, which
 * opens the system's file picker for several files; the tray over the box, a chip per file with its name, size, how far
 * its upload went, Remove, and on a failure the reason and Retry, and what the web refused before uploading; the drop
 * frame, a region (the whole chat, the composer) that shows "Drop files to attach them" over itself only while files
 * are dragged over it and hands them to the composer inside; and `pastedFiles`, the files (a screenshot) a paste
 * carries into the text area.
 */

/** The button that picks files: one hidden file input behind a labelled button, so it is reached and named as one. */
export function AttachButton({ onFiles, testId }: { onFiles: (files: File[]) => void; testId: string }) {
  const t = useTranslations("attachments");
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        tabIndex={-1}
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
        data-testid={`${testId}-files`}
      />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="shrink-0 text-muted-foreground"
        aria-label={t("attach", { max: MAX_ATTACHMENTS })}
        title={t("attach", { max: MAX_ATTACHMENTS })}
        onClick={() => input.current?.click()}
        data-testid={`${testId}-attach`}
      >
        <Paperclip aria-hidden="true" />
      </Button>
    </>
  );
}

/** The files of the message being written, and what the web refused to upload; nothing when there is neither. */
export function AttachmentTray({ files, testId }: { files: Attachments; testId: string }) {
  const t = useTranslations("attachments");
  const format = useFormatter();
  const size = useByteSize();
  if (files.items.length === 0 && files.refusals.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5 border-b px-2 py-2" data-testid={`${testId}-attachments`}>
      {files.items.length ? (
        <ul aria-label={t("list")} className="flex min-w-0 flex-wrap gap-1.5">
          {files.items.map((item) => (
            <AttachmentChip key={item.key} item={item} onRemove={() => files.remove(item.key)} onRetry={() => files.retry(item.key)} />
          ))}
        </ul>
      ) : null}
      {files.refusals.map((refusal) => (
        <p key={refusal.kind} role="alert" className="flex items-start gap-1.5 px-1 text-xs font-medium text-danger" data-testid="attachment-refused" data-kind={refusal.kind}>
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          {refusal.kind === "big"
            ? t("tooBig", { names: format.list(refusal.names, { type: "conjunction" }), max: size(MAX_ATTACHMENT_BYTES) })
            : t("tooMany", { names: format.list(refusal.names, { type: "conjunction" }), max: MAX_ATTACHMENTS })}
        </p>
      ))}
    </div>
  );
}

function AttachmentChip({ item, onRemove, onRetry }: { item: Upload; onRemove: () => void; onRetry: () => void }) {
  const t = useTranslations("attachments");
  const size = useByteSize();
  const name = baseName(item.file.name);
  const type = item.file.type || "application/octet-stream";
  const percent = item.file.size ? Math.min(100, Math.round((item.loaded / item.file.size) * 100)) : item.status === "done" ? 100 : 0;
  const reason =
    item.error?.kind === "big" ? t("failedBig", { max: size(MAX_ATTACHMENT_BYTES) }) : item.error?.kind === "network" ? t("failedNetwork") : (item.error?.message ?? t("failed"));
  return (
    <li
      className={cn("flex max-w-full min-w-0 items-center gap-2 rounded-sm border bg-card py-1 pr-1 pl-2.5", item.status === "failed" && "border-danger-solid")}
      data-testid="attachment-chip"
      data-status={item.status}
      data-name={name}
    >
      <OutputKindIcon output={{ viewer: viewerOf(type), name, media_type: type }} className="size-4 shrink-0 text-muted-foreground" />
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[13px] leading-[18px] font-medium text-foreground" title={item.file.name}>
          {name}
        </span>
        <span className="flex items-center gap-1.5 text-xs text-fg-subtle tabular-nums">
          {size(item.file.size)}
          {item.status === "uploading" ? (
            <span
              role="progressbar"
              aria-label={t("progress", { name })}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              className="relative h-1 w-12 overflow-hidden rounded-full bg-muted"
              data-testid="attachment-progress"
            >
              <span className="absolute inset-y-0 left-0 bg-brand transition-[width]" style={{ width: `${percent}%` }} />
            </span>
          ) : null}
          {item.status === "failed" ? (
            <span className="min-w-0 truncate font-medium text-danger" data-testid="attachment-error">
              {reason}
            </span>
          ) : null}
        </span>
      </span>
      {item.status === "failed" ? (
        <Button type="button" variant="ghost" size="icon-sm" aria-label={t("retry", { name })} title={t("retry", { name })} onClick={onRetry} data-testid="attachment-retry">
          <RotateCw aria-hidden="true" />
        </Button>
      ) : null}
      <Button type="button" variant="ghost" size="icon-sm" aria-label={t("remove", { name })} title={t("remove", { name })} onClick={onRemove} data-testid="attachment-remove">
        <X aria-hidden="true" />
      </Button>
    </li>
  );
}

/** The files a paste carries (a screenshot copied, files copied in a file manager); [] for a paste of text only. */
export function pastedFiles(event: ClipboardEvent<HTMLElement>): File[] {
  const data = event.clipboardData;
  if (!data) return [];
  const files = Array.from(data.files ?? []);
  if (files.length) return files;
  return Array.from(data.items ?? []).flatMap((item) => {
    const file = item.kind === "file" ? item.getAsFile() : null;
    return file ? [file] : [];
  });
}

type Register = (take: ((files: File[]) => void) | null) => void;

const DropTarget = createContext<Register | null>(null);

const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes("Files");

/**
 * A region files can be dropped on: while files are dragged over it, "Drop files to attach them" covers it; dropped,
 * they go to the composer inside that called `useFileDrop`. Text or a link dragged over it does nothing.
 */
export function FileDropFrame({ children, className, ...props }: ComponentProps<"div">) {
  const t = useTranslations("attachments");
  const take = useRef<((files: File[]) => void) | null>(null);
  const depth = useRef(0);
  const [over, setOver] = useState(false);
  const register = useCallback<Register>((next) => {
    take.current = next;
  }, []);
  const leave = useCallback(() => {
    depth.current = 0;
    setOver(false);
  }, []);
  // A drag that ends anywhere (dropped elsewhere, cancelled with Esc) takes the overlay away.
  useEffect(() => {
    window.addEventListener("dragend", leave);
    window.addEventListener("drop", leave);
    return () => {
      window.removeEventListener("dragend", leave);
      window.removeEventListener("drop", leave);
    };
  }, [leave]);
  return (
    <DropTarget.Provider value={register}>
      <div
        // Positioned only while files are dragged, for the overlay: a frame around a composer under a scrolling panel
        // must not paint over what that panel shows.
        className={cn(over && "relative", className)}
        onDragEnter={(event) => {
          if (!hasFiles(event) || !take.current) return;
          event.preventDefault();
          depth.current += 1;
          setOver(true);
        }}
        onDragOver={(event) => {
          if (!hasFiles(event) || !take.current) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={(event) => {
          if (!hasFiles(event) || !take.current) return;
          depth.current = Math.max(depth.current - 1, 0);
          if (depth.current === 0) setOver(false);
        }}
        onDrop={(event) => {
          if (!hasFiles(event) || !take.current) return;
          event.preventDefault();
          leave();
          take.current(Array.from(event.dataTransfer.files));
        }}
        {...props}
      >
        {children}
        {over ? (
          <div
            className="pointer-events-none absolute inset-0 z-20 grid place-items-center rounded-[inherit] border-2 border-dashed border-brand bg-background/90 p-4"
            data-testid="file-drop-overlay"
          >
            <p className="flex items-center gap-2 text-sm font-medium text-brand">
              <UploadIcon className="size-5 shrink-0" aria-hidden="true" />
              {t("drop")}
            </p>
          </div>
        ) : null}
      </div>
    </DropTarget.Provider>
  );
}

/** Takes the files dropped on the nearest `FileDropFrame` around the composer; false when there is none. */
export function useFileDrop(take: (files: File[]) => void): boolean {
  const register = useContext(DropTarget);
  useEffect(() => {
    if (!register) return;
    register(take);
    return () => register(null);
  }, [register, take]);
  return register !== null;
}
