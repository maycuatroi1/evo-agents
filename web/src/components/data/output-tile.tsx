"use client";

import {
  AppWindow,
  AudioLines,
  File,
  FileCode,
  FileSpreadsheet,
  FileText,
  Film,
  ImageIcon,
  ImageOff,
  type LucideIcon,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { baseName, extensionOf, type ViewedFile, textKind } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { useByteSize } from "./byte-size";

/**
 * The faces of an output (lib/outputs.ts): its icon by how it is viewed, an image's thumbnail loaded lazily from the
 * hub's own origin (the CSP's img-src 'self'), its size in words, and the two buttons that open it in the viewer: a tile
 * of the run page's grid and a chip of a strip (a trace row, a question, a chat message, a notice).
 */

const TEXT_ICON = { markdown: FileText, plain: FileText, json: FileCode, code: FileCode, csv: FileSpreadsheet } as const;

/** The icon of an output by how the web views it, text by what it holds: decorative, beside the words that name it. */
export function OutputKindIcon({ output, className }: { output: Pick<ViewedFile, "viewer" | "name" | "media_type">; className?: string }) {
  const Icon = ICONS[output.viewer === "text" ? textKind(output) : output.viewer];
  return <Icon className={className} aria-hidden="true" />;
}

const ICONS: Record<Exclude<ViewedFile["viewer"], "text"> | keyof typeof TEXT_ICON, LucideIcon> = {
  image: ImageIcon,
  video: Film,
  audio: AudioLines,
  pdf: FileText,
  html: AppWindow,
  none: File,
  ...TEXT_ICON,
};


/** What a screen reader hears for an output's button: "Open screenshot.png, image, 34.5 kB". */
export function useOutputName() {
  const t = useTranslations("outputs");
  const size = useByteSize();
  return (output: ViewedFile) => t("open", { name: baseName(output.name), kind: t(`viewer.kind.${output.viewer}`), size: size(output.size) });
}

/**
 * The picture of an output: an image's own bytes, lazily, cropped to its box; any other kind its icon and extension. An
 * image the browser cannot draw shows the broken-image icon. Decorative: the button around it carries the name.
 */
export function OutputThumb({ output, compact = false }: { output: ViewedFile; compact?: boolean }) {
  const [failed, setFailed] = useState(false);
  if (output.viewer === "image" && !failed) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- the bytes come from the hub's API, which next/image does not optimise
      <img
        src={output.url}
        alt=""
        loading="lazy"
        decoding="async"
        draggable={false}
        onError={() => setFailed(true)}
        className="size-full object-cover"
        data-testid="output-thumb-image"
      />
    );
  }
  const ext = extensionOf(output.name);
  return (
    <span className="flex size-full flex-col items-center justify-center gap-1 text-muted-foreground" data-testid="output-thumb-icon">
      {failed ? <ImageOff className={compact ? "size-4" : "size-7"} aria-hidden="true" /> : <OutputKindIcon output={output} className={compact ? "size-4" : "size-7"} />}
      {!compact && ext ? <span className="max-w-full truncate px-1 font-mono text-[11px] leading-4 uppercase">{ext}</span> : null}
    </span>
  );
}

const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** A tile of the run page's grid: the picture over the name, its size and when it came. Opens the viewer. */
export function OutputTile({ output, onOpen }: { output: ViewedFile; onOpen: () => void }) {
  const format = useFormatter();
  const size = useByteSize();
  const name = useOutputName();
  const shown = baseName(output.name);
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={name(output)}
      title={output.caption ?? output.name}
      className={cn(
        "group/tile flex w-full min-w-0 cursor-pointer flex-col overflow-hidden rounded-md border bg-card text-left shadow-raised transition-colors hover:bg-accent",
        FOCUS,
      )}
      data-testid="output-tile"
      data-output-id={output.id}
      data-viewer={output.viewer}
      data-source={output.source}
    >
      <span className="relative block aspect-[4/3] w-full overflow-hidden border-b bg-muted">
        <OutputThumb output={output} />
      </span>
      <span className="flex min-w-0 flex-col gap-0.5 px-2.5 py-2">
        <span className="truncate text-[13px] leading-[18px] font-medium text-foreground">{shown}</span>
        <span className="truncate text-xs text-fg-subtle tabular-nums">
          {size(output.size)}, {format.dateTime(new Date(output.created_at), { timeStyle: "short" })}
        </span>
      </span>
    </button>
  );
}

/**
 * A chip of a strip: an image as a 56 px thumbnail, any other file as its icon, name and size on one line. Opens the
 * viewer. 44 px tall at the least under 768 px.
 */
export function OutputChip({ output, onOpen }: { output: ViewedFile; onOpen: () => void }) {
  const size = useByteSize();
  const name = useOutputName();
  const image = output.viewer === "image";
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={name(output)}
      title={output.caption ?? output.name}
      className={cn(
        "flex max-w-full min-w-0 cursor-pointer items-center overflow-hidden rounded-sm border bg-card text-left transition-colors hover:bg-accent",
        image ? "h-14 w-20 justify-center bg-muted p-0" : "h-9 gap-2 px-2.5 max-md:min-h-11",
        FOCUS,
      )}
      data-testid="output-chip"
      data-output-id={output.id}
      data-viewer={output.viewer}
    >
      {image ? (
        <OutputThumb output={output} />
      ) : (
        <>
          <OutputKindIcon output={output} className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 truncate text-[13px] leading-[18px] font-medium text-foreground">{baseName(output.name)}</span>
          <span className="shrink-0 text-xs text-fg-subtle tabular-nums">{size(output.size)}</span>
        </>
      )}
    </button>
  );
}
