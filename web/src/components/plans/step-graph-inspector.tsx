"use client";

import { MousePointerClick } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useState } from "react";

import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/use-media-query";

import { type InspectorProps, StepInspector } from "./step-inspector";

/**
 * Where the inspector sits: from 1024 px a column on the right of the canvas, in the same card, with an empty state
 * until a step is selected; below, a bottom sheet that opens with the selection, takes the focus and gives it back to
 * the step when it closes (Esc, its close button or a tap outside).
 */

/** Tailwind's lg breakpoint (64rem, 1024 px). */
export const WIDE_QUERY = "(min-width: 64rem)";

export function useWideInspector(): boolean {
  return useMediaQuery(WIDE_QUERY);
}

type FrameProps = Omit<InspectorProps, "node" | "heading"> & {
  node: InspectorProps["node"] | null;
  /** Called after the sheet closed, to put the focus back on the step it showed. */
  onClosed: () => void;
};

const HEADING = "text-[15px] leading-[22px] font-semibold text-pretty [overflow-wrap:anywhere]";

export function InspectorPanel({ node, onClose, ...rest }: Omit<FrameProps, "onClosed">) {
  const t = useTranslations("plans.graph");
  // Esc anywhere in the column closes it and puts the focus back on the step, as on the canvas.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape" || !node) return;
    event.preventDefault();
    onClose();
  };
  return (
    <aside
      aria-label={t("inspector")}
      className="flex w-80 shrink-0 flex-col overflow-y-auto overscroll-contain border-l bg-card xl:w-96"
      onKeyDown={onKeyDown}
      data-testid="graph-inspector"
      data-layout="panel"
    >
      {node ? (
        <StepInspector
          key={node.key}
          node={node}
          onClose={onClose}
          heading={(title) => (
            <h3 className={HEADING} data-testid="inspector-title">
              {title}
            </h3>
          )}
          {...rest}
        />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center" data-testid="inspector-empty">
          <MousePointerClick className="size-5 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm font-medium">{t("inspectorEmpty")}</p>
          <p className="text-xs text-muted-foreground">{t("inspectorEmptyHint")}</p>
        </div>
      )}
    </aside>
  );
}

export function InspectorSheet({ node: selected, onClose, onClosed, ...rest }: FrameProps) {
  const t = useTranslations("plans.graph");
  // The step shown stays while the sheet slides away, rather than the sheet emptying first.
  const [node, setNode] = useState(selected);
  if (selected && selected !== node) setNode(selected);
  return (
    <Sheet open={selected !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[85svh] gap-0 overflow-y-auto overscroll-contain rounded-t-lg p-0 pb-[env(safe-area-inset-bottom)]"
        onOpenAutoFocus={(event) => {
          // The focus lands on the step's title, so a screen reader starts with it rather than with "Previous step".
          const title = (event.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>('[data-testid="inspector-title"]');
          if (!title) return;
          event.preventDefault();
          title.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          onClosed();
        }}
        data-testid="graph-inspector"
        data-layout="sheet"
      >
        {node ? (
          <>
            <SheetDescription className="sr-only">{t("sheetDescription", { key: node.key })}</SheetDescription>
            <StepInspector
              key={node.key}
              node={node}
              onClose={onClose}
              heading={(title) => (
                <SheetTitle className={HEADING} data-testid="inspector-title" tabIndex={-1}>
                  {title}
                </SheetTitle>
              )}
              {...rest}
            />
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
