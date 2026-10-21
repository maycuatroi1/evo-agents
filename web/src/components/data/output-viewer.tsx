"use client";

import { ChevronLeft, ChevronRight, Download, Maximize2, X, ZoomIn, ZoomOut } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, type ReactNode, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { baseName, downloadHref, type RunOutput } from "@/lib/outputs";

import { SafeMarkdown } from "./markdown";
import { OutputView, ZOOMS } from "./output-views";
import { SOURCE_ICON, useByteSize } from "./output-tile";

/**
 * The viewer of a list of outputs, a dialog over the page: the file's name as its title, its size, kind, source and
 * when it came under it, then the file itself as its kind is shown (output-views.tsx), and its caption under that.
 * The arrow keys (and on a touch screen a swipe) move to the file before or after, Esc closes, and an image zooms with
 * its buttons, `+`, `-` and `0`, or a double click. Download saves the file under its own name; `actions` adds what
 * the surface offers for the file shown (on a run's page: Show in trace, Delete). Full screen under 640 px. Loaded on
 * demand (`next/dynamic`), so a page that never opens a file carries none of it.
 */
export type OutputViewerProps = {
  outputs: readonly RunOutput[];
  /** The output shown; null closes the viewer. An id the list no longer holds (deleted) closes it too. */
  openId: number | null;
  onOpenChange: (id: number | null) => void;
  /** What the surface adds for the output shown; `show` moves the viewer to another output, or closes it (null). */
  actions?: (output: RunOutput, show: (id: number | null) => void) => ReactNode;
};

const SKIP_KEYS = new Set(["INPUT", "TEXTAREA", "SELECT", "VIDEO", "AUDIO"]);

export function OutputViewer({ outputs, openId, onOpenChange, actions }: OutputViewerProps) {
  const t = useTranslations("outputs.viewer");
  const tSource = useTranslations("outputs.source");
  const tKind = useTranslations("outputs.viewer.kind");
  const format = useFormatter();
  const size = useByteSize();
  const metaId = useId();
  const index = openId === null ? -1 : outputs.findIndex((output) => output.id === openId);
  // What the dialog shows while it closes: the last output it showed.
  const [last, setLast] = useState<RunOutput | null>(null);
  const current = index >= 0 ? outputs[index] : null;
  if (current && current !== last) setLast(current);
  const output = current ?? last;
  // An image's zoom belongs to the image shown: another output starts fitted.
  const [zoomOf, setZoomOf] = useState({ id: -1, zoom: 1 });
  const zoom = output && zoomOf.id === output.id ? zoomOf.zoom : 1;
  const setZoom = (next: number) => output && setZoomOf({ id: output.id, zoom: next });
  if (!output) return null;

  const total = outputs.length;
  const go = (step: number) => {
    if (index < 0 || total < 2) return;
    onOpenChange(outputs[(index + step + total) % total].id);
  };
  const image = output.viewer === "image";
  const level = ZOOMS.indexOf(zoom);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (SKIP_KEYS.has(target.tagName) || target.isContentEditable || event.altKey || event.metaKey || event.ctrlKey) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      go(event.key === "ArrowRight" ? 1 : -1);
    } else if (image && (event.key === "+" || event.key === "=")) setZoom(ZOOMS[Math.min(level + 1, ZOOMS.length - 1)]);
    else if (image && event.key === "-") setZoom(ZOOMS[Math.max(level - 1, 0)]);
    else if (image && event.key === "0") setZoom(1);
  };
  const SourceIcon = SOURCE_ICON[output.source];

  return (
    <Dialog open={current !== null} onOpenChange={(open) => (open ? undefined : onOpenChange(null))}>
      <DialogContent
        showCloseButton={false}
        aria-describedby={metaId}
        onKeyDown={onKeyDown}
        className="flex h-dvh max-h-dvh w-full max-w-full flex-col gap-0 overflow-hidden rounded-none p-0 sm:h-[min(92dvh,64rem)] sm:max-w-[min(96vw,84rem)] sm:rounded-lg"
        data-testid="output-viewer"
        data-output-id={output.id}
        data-viewer={output.viewer}
      >
        <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-3 py-2.5 sm:px-4">
          <div className="flex min-w-0 flex-1 basis-48 flex-col gap-0.5">
            <DialogTitle className="truncate text-[15px] leading-[22px] font-semibold" title={output.name} data-testid="output-viewer-name">
              {baseName(output.name)}
            </DialogTitle>
            <DialogDescription id={metaId} asChild>
              <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-fg-subtle tabular-nums" data-testid="output-viewer-meta">
                <span>{t("kindSize", { kind: tKind(output.viewer), size: size(output.size) })}</span>
                <span className="inline-flex items-center gap-1">
                  <SourceIcon className="size-3.5" aria-hidden="true" />
                  {tSource(output.source)}
                </span>
                <time dateTime={output.created_at} title={format.dateTime(new Date(output.created_at), { dateStyle: "full", timeStyle: "long" })}>
                  {format.dateTime(new Date(output.created_at), { dateStyle: "medium", timeStyle: "short" })}
                </time>
              </p>
            </DialogDescription>
          </div>
          <div className="flex flex-wrap items-center gap-1 max-sm:order-last max-sm:w-full max-sm:justify-between">
            {total > 1 ? (
              <div className="flex items-center gap-0.5" role="group" aria-label={t("navigation")}>
                <Button type="button" variant="ghost" size="icon" onClick={() => go(-1)} aria-label={t("previous")} aria-keyshortcuts="ArrowLeft" data-testid="output-viewer-previous">
                  <ChevronLeft aria-hidden="true" />
                </Button>
                <span className="min-w-14 text-center text-xs text-muted-foreground tabular-nums" aria-live="polite" data-testid="output-viewer-position">
                  {t("position", { index: index + 1, total })}
                </span>
                <Button type="button" variant="ghost" size="icon" onClick={() => go(1)} aria-label={t("next")} aria-keyshortcuts="ArrowRight" data-testid="output-viewer-next">
                  <ChevronRight aria-hidden="true" />
                </Button>
              </div>
            ) : null}
            {image ? (
              <div className="flex items-center gap-0.5" role="group" aria-label={t("zoom")}>
                <Button type="button" variant="ghost" size="icon" disabled={level <= 0} onClick={() => setZoom(ZOOMS[Math.max(level - 1, 0)])} aria-label={t("zoomOut")} aria-keyshortcuts="-" data-testid="output-viewer-zoom-out">
                  <ZoomOut aria-hidden="true" />
                </Button>
                <span className="min-w-11 text-center text-xs text-muted-foreground tabular-nums" data-testid="output-viewer-zoom">
                  {format.number(zoom, { style: "percent" })}
                </span>
                <Button type="button" variant="ghost" size="icon" disabled={level >= ZOOMS.length - 1} onClick={() => setZoom(ZOOMS[Math.min(level + 1, ZOOMS.length - 1)])} aria-label={t("zoomIn")} aria-keyshortcuts="+" data-testid="output-viewer-zoom-in">
                  <ZoomIn aria-hidden="true" />
                </Button>
                <Button type="button" variant="ghost" size="icon" disabled={zoom === 1} onClick={() => setZoom(1)} aria-label={t("fit")} aria-keyshortcuts="0" className="max-md:hidden">
                  <Maximize2 aria-hidden="true" />
                </Button>
              </div>
            ) : null}
            <div className="flex items-center gap-1">
              <Button asChild variant="outline" size="sm" className="max-md:min-w-11">
                <a href={downloadHref(output.url)} download={baseName(output.name)} data-testid="output-viewer-download">
                  <Download aria-hidden="true" />
                  <span className="max-md:sr-only">{t("download")}</span>
                </a>
              </Button>
              {actions?.(output, onOpenChange)}
            </div>
          </div>
          <Button type="button" variant="ghost" size="icon" onClick={() => onOpenChange(null)} aria-label={t("close")} data-testid="output-viewer-close">
            <X aria-hidden="true" />
          </Button>
        </header>
        <div className="relative min-h-0 flex-1 bg-muted" data-testid="output-viewer-body">
          <OutputView key={output.id} output={output} zoom={zoom} onZoom={setZoom} onSwipe={go} />
        </div>
        {output.caption ? (
          <div className="max-h-32 shrink-0 overflow-y-auto border-t px-4 py-2.5" data-testid="output-viewer-caption">
            <SafeMarkdown className="text-[13px] leading-5 text-muted-foreground [&_p]:my-0 [&_p+p]:mt-2">{output.caption}</SafeMarkdown>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
