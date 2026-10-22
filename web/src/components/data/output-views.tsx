"use client";

import { ExternalLink, ImageOff, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { type PointerEvent, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { baseName, type RunOutput } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { Centered, NoPreview } from "./output-frame";
import { OutputText } from "./output-text";

/**
 * The file itself inside the viewer (output-viewer.tsx), by the kind the hub gave it (`viewer`):
 *
 * - an image fitted to the box, zoomed by ZOOMS with its width set from the box in container units (no measuring), and
 *   dragged with the mouse or scrolled to pan once zoomed; a double click zooms to 200 % and back;
 * - a video and an audio file in the browser's own player, seekable since the hub answers byte ranges;
 * - a PDF in the browser's own viewer in a frame (the hub serves it with frame-ancestors 'self'), with a way to open it
 *   in a tab of its own where a browser shows no PDF in a frame;
 * - HTML in a frame whose `sandbox` lets its scripts run but gives it no origin: the hub's answer carries the same
 *   sandbox and connect-src 'none', so the page reaches neither the hub nor the visitor's session;
 * - text, code, JSON, CSV and Markdown read from the hub and drawn as such (output-text.tsx);
 * - anything else a download.
 *
 * A horizontal swipe on a touch screen moves to the file before or after, unless the image is zoomed.
 */

/** The zoom steps of an image, 1 being the image fitted to the box. */
export const ZOOMS = [1, 1.5, 2, 3, 4];
const SWIPE_PX = 48;

type ViewProps = { output: RunOutput; zoom: number; onZoom: (zoom: number) => void; onSwipe: (step: number) => void };

export function OutputView({ output, zoom, onZoom, onSwipe }: ViewProps) {
  const start = useRef<{ x: number; y: number } | null>(null);
  const onPointerDown = (event: PointerEvent) => {
    start.current = event.pointerType === "mouse" ? null : { x: event.clientX, y: event.clientY };
  };
  const onPointerUp = (event: PointerEvent) => {
    const from = start.current;
    start.current = null;
    if (!from || zoom !== 1) return;
    const dx = event.clientX - from.x;
    const dy = event.clientY - from.y;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > 1.5 * Math.abs(dy)) onSwipe(dx < 0 ? 1 : -1);
  };
  return (
    <div className="absolute inset-0 flex flex-col" onPointerDown={onPointerDown} onPointerUp={onPointerUp} onPointerCancel={() => {
        start.current = null;
      }}>
      <Body output={output} zoom={zoom} onZoom={onZoom} />
    </div>
  );
}

function Body({ output, zoom, onZoom }: Omit<ViewProps, "onSwipe">) {
  const t = useTranslations("outputs.viewer");
  switch (output.viewer) {
    case "image":
      return <ImageView output={output} zoom={zoom} onZoom={onZoom} />;
    case "video":
      return (
        <Centered>
          <video src={output.url} controls preload="metadata" playsInline className="max-h-full max-w-full bg-black" data-testid="output-video" />
        </Centered>
      );
    case "audio":
      return (
        <Centered>
          <audio src={output.url} controls preload="metadata" className="w-full max-w-lg" data-testid="output-audio" />
        </Centered>
      );
    case "pdf":
      return (
        <>
          <iframe src={output.url} title={t("frameTitle", { name: baseName(output.name) })} className="min-h-0 w-full flex-1 border-0 bg-white" data-testid="output-pdf" />
          <p className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t bg-card px-4 py-2 text-xs text-muted-foreground">
            {t("pdfNote")}
            <Button asChild variant="link" size="sm" className="h-auto px-0">
              <a href={output.url} target="_blank" rel="noopener noreferrer" data-testid="output-pdf-tab">
                <ExternalLink aria-hidden="true" />
                {t("openTab")}
              </a>
            </Button>
          </p>
        </>
      );
    case "html":
      return (
        <>
          <iframe
            src={output.url}
            title={t("frameTitle", { name: baseName(output.name) })}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            className="min-h-0 w-full flex-1 border-0 bg-white"
            data-testid="output-html"
          />
          <p className="flex shrink-0 items-center gap-2 border-t bg-card px-4 py-2 text-xs text-muted-foreground" data-testid="output-html-note">
            <ShieldCheck className="size-3.5 shrink-0" aria-hidden="true" />
            {t("sandboxNote")}
          </p>
        </>
      );
    case "text":
      return <OutputText output={output} />;
    case "none":
      return <NoPreview output={output}>{t("noPreview")}</NoPreview>;
  }
}

function ImageView({ output, zoom, onZoom }: Omit<ViewProps, "onSwipe">) {
  const t = useTranslations("outputs.viewer");
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [failed, setFailed] = useState(false);
  if (failed) return <NoPreview output={output} icon={ImageOff}>{t("imageFailed")}</NoPreview>;
  // Fitted, the image takes the most of the box (less its 1 rem of padding a side) its ratio allows, never more than
  // its own width; zoomed, a multiple of that.
  const width = size
    ? `calc(min(100cqw - 2rem, (100cqh - 2rem) * ${size.width / size.height}, ${size.width}px) * ${zoom})`
    : undefined;
  const zoomed = zoom > 1;
  const onPointerDown = (event: PointerEvent) => {
    const element = box.current;
    if (!zoomed || event.pointerType !== "mouse" || !element) return;
    drag.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop };
  };
  const onPointerMove = (event: PointerEvent) => {
    const from = drag.current;
    const element = box.current;
    if (!from || !element) return;
    element.scrollLeft = from.left - (event.clientX - from.x);
    element.scrollTop = from.top - (event.clientY - from.y);
  };
  return (
    <div
      ref={box}
      className={cn("min-h-0 flex-1 overflow-auto overscroll-contain [container-type:size]", zoomed ? "cursor-grab active:cursor-grabbing" : "touch-pan-y")}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={() => {
        drag.current = null;
      }}
      onPointerLeave={() => {
        drag.current = null;
      }}
      data-testid="output-image-box"
      data-zoom={zoom}
    >
      <div className="flex h-max min-h-full w-max min-w-full items-center justify-center p-4">
        {/* eslint-disable-next-line @next/next/no-img-element -- the bytes come from the hub's API, which next/image does not optimise */}
        <img
          src={output.url}
          alt={output.caption ?? baseName(output.name)}
          draggable={false}
          decoding="async"
          onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth || 1, height: event.currentTarget.naturalHeight || 1 })}
          onError={() => setFailed(true)}
          onDoubleClick={() => onZoom(zoomed ? 1 : 2)}
          style={width ? { width, maxWidth: "none" } : undefined}
          className={cn("h-auto select-none", !size && "max-h-full max-w-full")}
          data-testid="output-image"
        />
      </div>
    </div>
  );
}
