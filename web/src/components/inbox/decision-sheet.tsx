"use client";

import { useQuery } from "@tanstack/react-query";
import { SearchX, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, type RefObject, useEffect, useRef, useState } from "react";

import { InFrame } from "@/components/data/data-card";
import { ApiErrorState, LoadingState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";

import { DecisionCard } from "./decision-view";
import { useInboxViewer } from "./hooks";
import { decisionQuery, locateDecisionQuery } from "./queries";

/** The decision a sheet shows: its id, and its project when the caller knows it (Home, a notification on the page). */
export type DecisionTarget = {
  id: number;
  project: string | null;
  /** When its run parks, if the caller holds it already (the Home's overview). */
  parksAt?: string | null;
};

function sameTarget(a: DecisionTarget | null, b: DecisionTarget | null): boolean {
  return a?.id === b?.id && a?.project === b?.project && a?.parksAt === b?.parksAt;
}

/**
 * The sheet's content: the decision read from its project, or, for a link that names only the decision, asked of each
 * project the visitor holds a grant on at once. The question's heading takes focus once it shows, unless the visitor
 * has already moved on inside the sheet.
 */
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
  const viewer = useInboxViewer();
  const located = useQuery({
    ...locateDecisionQuery(browserApi, target.id, viewer.projects),
    enabled: target.project === null && viewer.login !== null,
  });
  const project = target.project ?? located.data?.project ?? null;
  const decision = useQuery({
    ...decisionQuery(browserApi, project ?? "-", target.id),
    enabled: project !== null,
    initialData: located.data && located.data.project === project ? located.data.decision : undefined,
  });
  const loaded = decision.data !== undefined;
  useEffect(() => {
    if (!loaded) return;
    const active = document.activeElement;
    if (active === null || active === document.body || active === contentRef.current) headingRef.current?.focus();
  }, [loaded, headingRef, contentRef]);

  const notFound =
    (target.project === null && located.isSuccess && located.data === null) ||
    (decision.isError && decision.error.info.status === 404) ||
    (target.project === null && viewer.login !== null && viewer.projects.length === 0);

  let body: ReactNode;
  if (notFound) {
    body = (
      <StatePanel icon={SearchX} title={t("notFoundTitle", { id: target.id })} description={t("notFoundDescription")} testId="decision-not-found" />
    );
  } else if (decision.data) {
    body = <DecisionCard decision={decision.data} where="inbox" headingRef={headingRef} parksAt={target.parksAt} flush />;
  } else if (decision.isError || located.isError) {
    const error = decision.error ?? located.error;
    body = error ? <ApiErrorState error={error.info} onRetry={() => void (decision.isError ? decision.refetch() : located.refetch())} /> : null;
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
 */
export function DecisionSheet({
  target,
  onClose,
  returnFocus,
}: {
  target: DecisionTarget | null;
  onClose: () => void;
  returnFocus?: (id: number) => HTMLElement | null;
}) {
  const t = useTranslations("inbox.sheet");
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
        className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl"
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
      >
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
      </SheetContent>
    </Sheet>
  );
}
