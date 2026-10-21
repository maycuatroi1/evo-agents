"use client";

import {
  ArrowUpRight,
  CalendarCheck,
  CalendarX,
  ClipboardCheck,
  GitMerge,
  Goal,
  Lightbulb,
  Lock,
  type LucideIcon,
  MessageCircle,
  MessageSquare,
  MessagesSquare,
  PowerOff,
  SearchCheck,
  Signpost,
  SlidersHorizontal,
  Table,
  TriangleAlert,
  FileText,
} from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { MetricStrip } from "@/components/data/metric-strip";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import {
  BOARD_GROUPS,
  type BoardGroup,
  type BoardItem,
  type BoardKind,
  decisionBoardQuery,
  type DecisionBoard,
  itemHref,
  LOOP_REFRESH_MS,
} from "@/lib/loop-queries";

import { useLoopViewer } from "./hooks";
import { boardGroups } from "./model";

const KIND_ICONS: Record<BoardKind, LucideIcon> = {
  goal_review: CalendarCheck,
  goal_overdue: CalendarX,
  baseline: ClipboardCheck,
  claim_proposal: Lightbulb,
  policy_proposal: SlidersHorizontal,
  goal_proposal: Goal,
  merge_breaker: PowerOff,
  loop_breaker: PowerOff,
  loop_decision: MessageSquare,
  run_decision: MessageSquare,
  author_question: MessageCircle,
  merge_held: Lock,
  merge_manual: GitMerge,
  merge_sample: SearchCheck,
  srs_review: ClipboardCheck,
  srs_finding: MessagesSquare,
};

const GROUP_ICONS: Record<BoardGroup, LucideIcon> = {
  goal: Goal,
  canvas: Table,
  policy: SlidersHorizontal,
  exception: TriangleAlert,
  srs: FileText,
};

/** One thing that waits: what, since when, who may decide it, and the page where it is decided. */
function BoardRow({ project, item, login }: { project: string; item: BoardItem; login: string | null }) {
  const t = useTranslations("loop.decide");
  const kind = useTranslations("loop.boardKind");
  const decidedBy = useTranslations("loop.decidedBy");
  const Icon = KIND_ICONS[item.kind];
  const mine = login !== null && item.deciders.includes(login);
  const who = item.deciders.length > 0 ? t("deciders", { rule: decidedBy(item.decided_by), logins: item.deciders.join(", ") }) : t("nobody", { rule: decidedBy(item.decided_by) });
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-2 px-4 py-3" data-testid="board-item" data-kind={item.kind} data-mine={mine ? "true" : "false"}>
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-sm bg-surface-sunken text-muted-foreground" aria-hidden="true">
        <Icon className="size-4" />
      </span>
      <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Tag>{kind(item.kind)}</Tag>
          {mine ? (
            <Tag className="bg-attention-soft text-attention" data-testid="board-item-mine">
              {t("yours")}
            </Tag>
          ) : null}
        </div>
        <p className="text-sm text-foreground [overflow-wrap:anywhere]" data-testid="board-item-title">
          {item.title}
        </p>
        <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-fg-subtle">
          {item.since ? (
            <span>
              {t("since")} <When value={item.since} short />
            </span>
          ) : null}
          <span data-testid="board-item-deciders">{who}</span>
        </p>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 max-sm:w-full max-sm:pl-10">
        {item.work_item !== null && item.work_item !== undefined ? (
          <Button asChild variant="ghost" size="sm">
            <Link href={itemHref(project, item.work_item)} data-testid="board-item-chain">
              {t("chain", { id: item.work_item })}
            </Link>
          </Button>
        ) : null}
        {item.link ? (
          <Button asChild variant="secondary" size="sm">
            <Link href={item.link as Route} aria-label={t("openLabel", { title: item.title })} data-testid="board-item-open">
              <ArrowUpRight aria-hidden="true" />
              {t("open")}
            </Link>
          </Button>
        ) : null}
      </div>
    </li>
  );
}

function GroupSection({ project, group, items, loop, login }: { project: string; group: BoardGroup; items: BoardItem[]; loop: boolean; login: string | null }) {
  const t = useTranslations("loop.decide");
  const Icon = GROUP_ICONS[group];
  const chained = group === "exception" || group === "srs";
  return (
    <section id={`decide-${group}`} aria-labelledby={`decide-${group}-title`} className="min-w-0 scroll-mt-20 rounded-md border bg-card shadow-raised" data-testid="board-group" data-group={group}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
        <h2 id={`decide-${group}-title`} className="text-[15px] leading-[22px] font-semibold">
          {t(`group.${group}`)}
        </h2>
        <span className="text-xs text-fg-subtle">{t(`groupNote.${group}`)}</span>
        <span
          className={`ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-medium tabular-nums ${items.length ? "bg-attention-soft text-attention" : "bg-muted text-muted-foreground"}`}
          data-testid="board-group-count"
        >
          <span aria-hidden="true">{items.length}</span>
          <span className="sr-only">{t("waiting", { count: items.length })}</span>
        </span>
      </div>
      {items.length === 0 ? (
        <p className="px-4 py-4 text-[13px] text-muted-foreground">{chained && !loop ? t("loopOff") : t("nothing")}</p>
      ) : (
        <ul className="divide-y">
          {items.map((item, index) => (
            <BoardRow key={`${item.kind}-${index}`} project={project} item={item} login={login} />
          ))}
        </ul>
      )}
    </section>
  );
}

function Board({ project, board, login }: { project: string; board: DecisionBoard; login: string | null }) {
  const t = useTranslations("loop.decide");
  const groups = boardGroups(board.items);
  if (board.items.length === 0) {
    return <EmptyState icon={Signpost} title={t("emptyTitle")} description={t("emptyText")} testId="board-empty" />;
  }
  return (
    <>
      <MetricStrip
        label={t("summary")}
        testId="board-summary"
        metrics={BOARD_GROUPS.map((group) => ({
          id: group,
          label: t(`group.${group}`),
          icon: GROUP_ICONS[group],
          value: board.counts[group] ?? 0,
          tone: "attention" as const,
          href: `#decide-${group}` as Route,
        }))}
      />
      {BOARD_GROUPS.map((group) => (
        <GroupSection key={group} project={project} group={group} items={groups.get(group) ?? []} loop={board.loop} login={login} />
      ))}
    </>
  );
}

/**
 * A project's Decisions page (/p/{project}/decide), for any member with a grant on it, kept current while it is open:
 * everything that waits for a person, the three gates first (the goals, the canvases, the policy), then the Loop's
 * exceptions and the SRS its chain waits on; each with since when, who may decide it, and a link to the page where it
 * is decided, whose buttons call the object's own route. The page itself decides nothing.
 */
export function DecidePage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.decide");
  const viewer = useLoopViewer(project);
  const state = useHubQuery({ ...decisionBoardQuery(browserApi, project), refetchInterval: LOOP_REFRESH_MS }, initialError, { live: true });
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="decide-page">
      <PageHeader title={t("title")} testId="decide-header" />
      <QueryView state={state} loading={<ListSkeleton rows={6} metrics={5} />}>
        {(board) => <Board project={project} board={board} login={viewer?.login ?? null} />}
      </QueryView>
    </div>
  );
}
