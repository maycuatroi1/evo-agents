"use client";

import { ArrowRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useMoney } from "@/components/data/facts";
import { Button } from "@/components/ui/button";
import type { WorkCard } from "@/lib/work";

import { agentCount } from "./task-model";
import { useWorkTime, WorkStatusPill } from "./work-look";

/**
 * The card of a task in the direct chat on Home (web/DESIGN.md, Chat home), where the hub's message that carries its
 * work card sits: its state, its title, what is left, how many agents work on it, its time and cost, and Open task,
 * which opens the task in Home's main pane. What the coordinator said meanwhile is the task's, not the chat's.
 */
export function TaskCard({ work, onOpen }: { work: WorkCard; onOpen: (chatId: number) => void }) {
  const t = useTranslations("chatHome.task");
  const money = useMoney();
  const time = useWorkTime();
  const id = useId();
  // Before its first hand-off the work has no chat of its own yet: the card says how it goes, and opens nothing.
  const chatId = work.work_chat_id;
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="flex w-full max-w-[46rem] min-w-0 flex-col gap-2 rounded-md border bg-card px-3 py-2.5 shadow-raised"
      data-testid="task-card"
      data-status={work.status}
      data-task={chatId ?? undefined}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <WorkStatusPill status={work.status} />
        <h3 id={`${id}-title`} className="min-w-[8rem] flex-1 text-sm font-semibold [overflow-wrap:anywhere]">
          {work.title}
        </h3>
      </div>
      {work.left ? (
        <p className="text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="task-card-left">
          <span className="text-muted-foreground">{t("left")} </span>
          {work.left}
        </p>
      ) : null}
      <p className="text-[13px] leading-[18px] text-muted-foreground tabular-nums" data-testid="task-card-figures">
        {t("cardFigures", { agents: agentCount(work), time: time(work.seconds), cost: money(work.usd) })}
      </p>
      {chatId != null ? (
        <Button type="button" variant="outline" size="sm" className="w-fit max-md:min-h-11" aria-label={t("openTitled", { title: work.title })} onClick={() => onOpen(chatId)} data-testid="task-card-open">
          {t("open")}
          <ArrowRight aria-hidden="true" />
        </Button>
      ) : null}
    </section>
  );
}
