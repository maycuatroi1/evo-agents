"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import { chatHref } from "@/lib/chats";
import type { WorkCard } from "@/lib/work";

import { useWaitingLine, useWorkTotals, WorkStatusPill } from "./work-look";

/**
 * The work card in the root thread (web/DESIGN.md, Work card): the hub's message there carries the card of the trace
 * the thread started, and each change of it comes as a `message_changed` event, so it stays live where the person
 * asked (Home, or the chat they asked in). It shows the title, the state, what the work waits for and Open work chat;
 * once done, Done with the runs, cost and time it took.
 */
export function WorkRootCard({ work }: { work: WorkCard }) {
  const t = useTranslations("chats.work.root");
  const waitingLine = useWaitingLine();
  const totals = useWorkTotals();
  const id = useId();
  const done = work.status === "done";
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="flex w-full max-w-[32rem] min-w-0 flex-col gap-2 rounded-md border bg-card px-3 py-2.5 shadow-raised"
      data-testid="work-root-card"
      data-status={work.status}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <h3 id={`${id}-title`} className="min-w-0 flex-1 truncate text-sm font-semibold" title={work.title}>
          {work.title}
        </h3>
        <WorkStatusPill status={work.status} />
      </div>
      <p className="text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="work-root-line">
        {done ? t("done", { totals: totals(work) }) : waitingLine(work)}
      </p>
      {!done ? (
        <p className="text-xs text-fg-subtle tabular-nums" data-testid="work-root-totals">
          {totals(work)}
        </p>
      ) : null}
      {work.work_chat_id !== null ? (
        <Button asChild variant="outline" size="sm" className="w-fit">
          <Link href={chatHref(work.work_chat_id)} data-testid="work-root-open">
            {t("open")}
            <ArrowRight aria-hidden="true" />
          </Link>
        </Button>
      ) : null}
    </section>
  );
}
