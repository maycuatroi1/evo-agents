"use client";

import { SearchX, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, type RefObject, useRef, useState } from "react";

import { InFrame } from "@/components/data/data-card";
import { ApiErrorState, LoadingState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

import { DecisionCard } from "./decision-view";
import { DecisionScreen } from "./mobile-decision";
import { type DecisionTarget, useTargetDecision } from "./target-decision";

export type { DecisionTarget };

function sameTarget(a: DecisionTarget | null, b: DecisionTarget | null): boolean {
  return a?.id === b?.id && a?.project === b?.project && a?.parksAt === b?.parksAt;
}

/** The sheet's content: the kit's DecisionCard edge to edge, or why it is not there. */
function SheetBody({
  target,
  headingRef,
  contentRef,
}: {
  target: DecisionTarget;
  headingRef: RefObject<HTMLHeadingElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
}) {
  const t = useTranslations("inbox.sheet");
  const { decision, notFound, error, retry } = useTargetDecision(target, headingRef, contentRef);

  let body: ReactNode;
  if (notFound) {
    body = (
      <StatePanel icon={SearchX} title={t("notFoundTitle", { id: target.id })} description={t("notFoundDescription")} testId="decision-not-found" />
    );
  } else if (decision.data) {
    body = <DecisionCard decision={decision.data} where="inbox" headingRef={headingRef} parksAt={target.parksAt} flush />;
  } else if (error) {
    body = <ApiErrorState error={error.info} onRetry={retry} />;
  } else {
    body = (
      <LoadingState className="p-4">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-6 w-56" />
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-18 w-full" />
        </div>
      </LoadingState>
    );
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" data-testid="decision-panel" data-decision-id={target.id}>
      <InFrame>{body}</InFrame>
    </div>
  );
}

/**
 * The sheet that answers a decision without leaving the page: from the Inbox's list (`/inbox?decision=ID`) and from
 * Home's Needs you. It slides in from the right, the whole width of a phone and 576 px from the sm breakpoint, over a
 * page that stays where it was. Its top bar names the decision and closes it; below is the kit's DecisionCard, edge to
 * edge, its answer form's footer kept in view at the foot. Esc, the close button or a click outside call `onClose`;
 * focus then goes back to what held it when the sheet opened, or, when that is gone or the sheet opened from the URL,
 * to what `returnFocus` names.
 *
 * With `screen` (the Inbox), under 768 px the sheet is the kit's MobileDecision instead: a screen of its own, as tall as
 * what the phone shows, with Back to Inbox in its top bar and the answer's buttons in a bar at its foot.
 */
export function DecisionSheet({
  target,
  onClose,
  returnFocus,
  screen = false,
}: {
  target: DecisionTarget | null;
  onClose: () => void;
  returnFocus?: (id: number) => HTMLElement | null;
  screen?: boolean;
}) {
  const t = useTranslations("inbox.sheet");
  const phone = useIsMobile();
  const asScreen = screen && phone;
  // The decision stays on screen while the sheet slides out.
  const [shown, setShown] = useState<DecisionTarget | null>(target);
  if (target !== null && !sameTarget(target, shown)) setShown(target);
  const heading = useRef<HTMLHeadingElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  return (
    <Sheet open={target !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        ref={content}
        side="right"
        showCloseButton={false}
        aria-describedby={undefined}
        className={cn(
          "gap-0 p-0 data-[side=right]:w-full",
          asScreen
            ? "bg-background data-[side=right]:h-dvh data-[side=right]:max-w-none data-[side=right]:border-l-0 data-[side=right]:sm:max-w-none"
            : "data-[side=right]:sm:max-w-xl",
        )}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          const active = document.activeElement;
          opener.current = active instanceof HTMLElement && active !== document.body ? active : null;
          (heading.current ?? content.current)?.focus();
        }}
        onCloseAutoFocus={(event) => {
          // Radix gives focus back only to a Sheet trigger, and this sheet opens from links, buttons and the URL.
          event.preventDefault();
          const back = opener.current?.isConnected ? opener.current : shown ? (returnFocus?.(shown.id) ?? null) : null;
          opener.current = null;
          back?.focus();
        }}
        data-testid="decision-sheet"
        data-decision-id={shown?.id}
        data-layout={asScreen ? "screen" : "sheet"}
      >
        {asScreen ? (
          shown ? (
            <DecisionScreen key={shown.id} target={shown} headingRef={heading} contentRef={content} />
          ) : (
            <SheetTitle className="sr-only">{t("titleUnknown")}</SheetTitle>
          )
        ) : (
          <>
            <div className="flex h-13 shrink-0 items-center gap-2 border-b px-4">
              <SheetTitle asChild>
                <p className="min-w-0 truncate text-[15px] leading-[22px] font-semibold">{shown ? t("title", { id: shown.id }) : t("titleUnknown")}</p>
              </SheetTitle>
              <SheetClose asChild>
                <Button type="button" variant="ghost" size="icon" className="ml-auto" aria-label={t("close")} data-testid="decision-close">
                  <X aria-hidden="true" />
                </Button>
              </SheetClose>
            </div>
            {shown ? <SheetBody key={shown.id} target={shown} headingRef={heading} contentRef={content} /> : null}
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
