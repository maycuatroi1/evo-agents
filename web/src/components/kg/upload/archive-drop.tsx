"use client";

import { FileArchive, TriangleAlert, Upload as UploadIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import { type DragEvent, useId, useRef, useState } from "react";

import { useByteSize } from "@/components/data/byte-size";
import { Button } from "@/components/ui/button";
import { ARCHIVE_ACCEPT, isArchive } from "@/lib/kg/upload";
import { cn } from "@/lib/utils";

const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes("Files");

/** Why a file dropped or chosen is not taken: more than one, not an archive the hub reads, or empty. */
export function refusal(files: File[]): { key: "oneFile" | "notArchive" | "empty"; name: string } | null {
  if (files.length !== 1) return { key: "oneFile", name: "" };
  const [file] = files;
  if (!isArchive(file.name)) return { key: "notArchive", name: file.name };
  if (file.size === 0) return { key: "empty", name: file.name };
  return null;
}

/**
 * The archive to upload: dropped on the frame, or chosen with the button (the file input under it, which the
 * keyboard and screen readers reach as "Archive to upload"). A file the hub does not read is refused here, before any
 * byte is hashed or sent. Once chosen, the frame names the file and its size, and offers another.
 */
export function ArchiveDrop({
  file,
  onFile,
  disabled = false,
}: {
  file: File | null;
  onFile: (file: File) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("kgUpload.drop");
  const size = useByteSize();
  const input = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const depth = useRef(0);
  const [over, setOver] = useState(false);
  const [refused, setRefused] = useState<ReturnType<typeof refusal>>(null);

  const take = (files: File[]) => {
    const why = refusal(files);
    setRefused(why);
    if (!why) onFile(files[0]);
  };

  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn(
          "relative flex flex-col items-center gap-3 rounded-md border border-dashed border-input bg-surface-sunken px-4 py-8 text-center transition-colors",
          over && "border-2 border-brand bg-background",
          disabled && "opacity-60",
        )}
        data-testid="kg-upload-drop"
        data-over={over || undefined}
        onDragEnter={(event) => {
          if (disabled || !hasFiles(event)) return;
          event.preventDefault();
          depth.current += 1;
          setOver(true);
        }}
        onDragOver={(event) => {
          if (disabled || !hasFiles(event)) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={(event) => {
          if (disabled || !hasFiles(event)) return;
          depth.current = Math.max(depth.current - 1, 0);
          if (depth.current === 0) setOver(false);
        }}
        onDrop={(event) => {
          if (disabled || !hasFiles(event)) return;
          event.preventDefault();
          depth.current = 0;
          setOver(false);
          take(Array.from(event.dataTransfer.files));
        }}
      >
        {file ? (
          <FileArchive className="size-8 text-muted-foreground" aria-hidden="true" />
        ) : (
          <UploadIcon className="size-8 text-muted-foreground" aria-hidden="true" />
        )}
        {file ? (
          <p className="flex max-w-full flex-col gap-0.5" data-testid="kg-upload-file">
            <span className="truncate font-mono text-sm font-medium text-foreground" title={file.name}>
              {file.name}
            </span>
            <span className="text-xs text-muted-foreground tabular-nums">{size(file.size)}</span>
          </p>
        ) : (
          <p className="flex flex-col gap-1">
            <span className="text-sm font-medium text-foreground">{over ? t("over") : t("title")}</span>
            <span id={hintId} className="text-xs text-muted-foreground">
              {t("hint")}
            </span>
          </p>
        )}
        <Button type="button" variant="outline" disabled={disabled} onClick={() => input.current?.click()} data-testid="kg-upload-choose">
          {file ? t("change") : t("choose")}
        </Button>
        <input
          ref={input}
          type="file"
          accept={ARCHIVE_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          aria-label={t("input")}
          aria-describedby={file ? undefined : hintId}
          disabled={disabled}
          data-testid="kg-upload-input"
          onChange={(event) => {
            take(Array.from(event.target.files ?? []));
            event.target.value = ""; // the same file chosen again (to resume) fires change again
          }}
        />
      </div>
      {refused ? (
        <p role="alert" className="flex items-start gap-1.5 text-sm font-medium text-danger" data-testid="kg-upload-refused" data-kind={refused.key}>
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {t(refused.key, { name: refused.name })}
        </p>
      ) : null}
    </div>
  );
}
