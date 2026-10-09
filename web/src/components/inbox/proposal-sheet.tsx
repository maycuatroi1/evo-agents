"use client";

import { queryOptions, useQuery } from "@tanstack/react-query";
import { ArrowRight, SearchX, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type RefObject, useEffect, useRef, useState } from "react";

import { AnswerButtons, AnswerDialog } from "@/components/curator/answer-dialog";
import { KindTag, LensTag, TierTag } from "@/components/curator/badges";
import { useCuratorViewer } from "@/components/curator/hooks";
import { ProposalBody } from "@/components/curator/proposal-detail";
import { TEXT_LINK } from "@/components/curator/parts";
import { type Proposal, type ProposalAction, proposalHref, proposalQuery } from "@/components/curator/queries";
import { InFrame } from "@/components/data/data-card";
import { runHref } from "@/components/runs/queries";
import { ApiErrorState, LoadingState, StatePanel } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import type { ApiSource } from "@/lib/queries";

import { useInboxViewer } from "./hooks";

/** A proposal the sheet shows: its id, and its project when the Inbox's list says it. */
export type ProposalTarget = { id: number; project: string | null };

type Located = { project: string; proposal: Proposal };

/**
 * A proposal opened from a link (`/inbox?proposal=ID`) that names no project: asked of each project the member holds a
 * grant on, at once; null when none holds it. Any failure but 403 and 404 is thrown when no project answered.
 */
const locateProposalQuery = (api: ApiSource, id: number, projects: readonly string[]) =>
  queryOptions({
    queryKey: ["me", "proposal-located", id, [...projects].sort()] as const,
    queryFn: async ({ signal }): Promise<Located | null> => {
      const answers = await Promise.allSettled(
        projects.map(async (project) => ({
          project,
          proposal: await call(
            api().GET("/v1/projects/{project}/curator/proposals/{proposal_id}", { params: { path: { project, proposal_id: id } }, signal }),
          ),
        })),
      );
      const found = answers.find((answer) => answer.status === "fulfilled");
      if (found && found.status === "fulfilled") return found.value;
      const failure = answers.find(
        (answer) => answer.status === "rejected" && !(isApiError(answer.reason) && (answer.reason.status === 404 || answer.reason.status === 403)),
      );
      if (failure && failure.status === "rejected") throw failure.reason;
      return null;
    },
    staleTime: Infinity, // a proposal never moves to another project
  });

function SheetBody({ target, headingRef }: { target: ProposalTarget; headingRef: RefObject<HTMLHeadingElement | null> }) {
  const t = useTranslations("inbox.proposal");
  const { projects } = useInboxViewer();
  const located = useQuery({ ...locateProposalQuery(browserApi, target.id, projects), enabled: target.project === null && projects.length > 0 });
  const project = target.project ?? located.data?.project ?? null;
  const proposal = useQuery({ ...proposalQuery(browserApi, project ?? "", target.id), enabled: project !== null });
  const viewer = useCuratorViewer(project ?? "");
  const admin = viewer?.role === "admin";
  const [action, setAction] = useState<ProposalAction | null>(null);
  const shown = proposal.data ?? located.data?.proposal ?? null;

  // Once the proposal is read, its heading takes focus, as a decision's question does in its sheet.
  const ready = shown !== null;
  useEffect(() => {
    if (ready) headingRef.current?.focus();
  }, [ready, headingRef]);

  if ((target.project === null && located.data === null) || (proposal.isError && proposal.error.status === 404)) {
    return <StatePanel icon={SearchX} title={t("notFoundTitle", { id: target.id })} description={t("notFoundDescription")} testId="proposal-not-found" />;
  }
  if (!shown || project === null) {
    const error = proposal.error ?? located.error;
    if (error) return <ApiErrorState error={error.info} onRetry={() => void (proposal.isError ? proposal.refetch() : located.refetch())} />;
    return (
      <LoadingState className="p-4">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </LoadingState>
    );
  }
  return (
    <div className="flex flex-col gap-4 p-4" data-testid="proposal-panel" data-proposal-id={shown.id} data-state={shown.state}>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge kind="proposal" status={shown.state} />
          <TierTag tier={shown.tier} />
          <KindTag kind={shown.kind} />
          <LensTag lens={shown.lens} />
        </div>
        <h2 ref={headingRef} tabIndex={-1} className="text-[15px] leading-[22px] font-semibold text-pretty outline-none [overflow-wrap:anywhere]" data-testid="proposal-sheet-title">
          {shown.title}
        </h2>
        <p className="text-[13px] text-muted-foreground">
          {t.rich("where", {
            project,
            run: shown.run_id,
            runLink: (chunks) => (
              <Link href={runHref(project, shown.run_id)} className={TEXT_LINK}>
                {chunks}
              </Link>
            ),
          })}
        </p>
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {admin ? <AnswerButtons proposal={shown} onAnswer={setAction} size="sm" /> : null}
          <Link
            href={proposalHref(project, shown.id)}
            className="ml-auto inline-flex min-h-7 items-center gap-1 text-[13px] font-medium text-brand underline-offset-4 hover:underline max-md:min-h-11"
            data-testid="proposal-sheet-open"
          >
            {t("openPage")}
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </Link>
        </div>
      </div>
      <ProposalBody project={project} proposal={shown} admin={admin} compact />
      {admin ? <AnswerDialog project={project} proposal={shown} action={action} onClose={() => setAction(null)} linkBack /> : null}
    </div>
  );
}

/**
 * The sheet that shows a tier 2 proposal of the Curator from the Inbox (`/inbox?proposal=ID`), as a decision opens in
 * its own sheet: from the right, the whole width of a phone and 576 px from the sm breakpoint, its top bar naming it
 * beside a close button, then the proposal with its evidence and draft plan, and for an admin of its project Accept,
 * Defer and Reject. Closing gives focus back to what opened it, or to the proposal's link in the list.
 */
export function ProposalSheet({
  target,
  onClose,
  returnFocus,
}: {
  target: ProposalTarget | null;
  onClose: () => void;
  returnFocus?: (id: number) => HTMLElement | null;
}) {
  const t = useTranslations("inbox.proposal");
  const [shown, setShown] = useState<ProposalTarget | null>(target);
  if (target !== null && (target.id !== shown?.id || target.project !== shown?.project)) setShown(target);
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
          event.preventDefault();
          const back = opener.current?.isConnected ? opener.current : shown ? (returnFocus?.(shown.id) ?? null) : null;
          opener.current = null;
          back?.focus();
        }}
        data-testid="proposal-sheet"
        data-proposal-id={shown?.id}
      >
        <div className="flex h-13 shrink-0 items-center gap-2 border-b px-4">
          <SheetTitle asChild>
            <p className="min-w-0 truncate text-[15px] leading-[22px] font-semibold">{shown ? t("title", { id: shown.id }) : t("titleUnknown")}</p>
          </SheetTitle>
          <SheetClose asChild>
            <Button type="button" variant="ghost" size="icon" className="ml-auto" aria-label={t("close")} data-testid="proposal-close">
              <X aria-hidden="true" />
            </Button>
          </SheetClose>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <InFrame>{shown ? <SheetBody key={shown.id} target={shown} headingRef={heading} /> : null}</InFrame>
        </div>
      </SheetContent>
    </Sheet>
  );
}
