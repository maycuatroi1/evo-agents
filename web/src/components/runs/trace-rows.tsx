"use client";

import { ChevronRight, Circle, CircleCheck, CircleDot, CircleX, Info, ListChecks, MessageSquare, User } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { OutputStrip } from "@/components/data/output-strip";
import { eventFiles, runFiles } from "@/lib/attachments";
import { cn } from "@/lib/utils";

import type { RepoWeb } from "./forge";
import { LinkedText } from "./links";
import type { DescribeMove } from "./log-model";
import { decisionAnchor, decisionHref } from "./queries";
import type { TraceItem } from "./trace-model";
import { Row, Who } from "./trace-row";
import { TermBlock } from "./trace-term";
import { useTraceOutputs } from "./use-run-outputs";

/**
 * The rows of the kit's AgentTrace (agent-trace.tsx) that are not the agent's words or its tool calls: the plan as a
 * checklist, "Asked you" for a decision, a person's message, a move between states and a line of the hub's. A line of
 * the hub's that recorded files no tool call shows (the run's output folder) shows them as a strip.
 */

export type TraceContext = {
  runId: number;
  /** The run's project: where the files a person's message carries are read. */
  project?: string;
  /** The login of the run's owner, whom the agent asks. */
  owner: string;
  /** The visitor's login, to say "You". */
  viewer: string | null;
  describe: DescribeMove;
  /** Decisions open now whose card is on this page, which "Asked you" links to; the others link to the Inbox. */
  openDecisions: ReadonlySet<number>;
  /** An author run's: opens a decision's card in the Chat tab, where every decision of the run is. */
  showDecision?: (id: number) => void;
  /** The agent is at work: the trace ends with the typing dots. */
  working: boolean;
  /** The run may still write events. */
  active: boolean;
  /** The web page of the run's repo on a branch, which a step report's branch and commits link to. */
  webOfBranch?: (branch: string | null) => RepoWeb | null;
};

/** The files the hub's line at `seq` recorded that no tool call of the trace shows. */
function RecordedFiles({ seq }: { seq: number }) {
  const t = useTranslations("outputs.strip");
  const outputs = useTraceOutputs();
  const files = outputs?.index?.bySystem.get(seq);
  if (!outputs || !files?.length) return null;
  return <OutputStrip outputs={files} label={t("recorded")} onOpen={outputs.open} className="mt-1 mb-1" testId="trace-recorded-files" />;
}

export function PlanRow({ item }: { item: Extract<TraceItem, { type: "plan" }> }) {
  const t = useTranslations("runs.detail.trace");
  const done = item.entries.filter((entry) => entry.status === "completed").length;
  return (
    <Row
      icon={ListChecks}
      look="agent"
      at={item.at}
      head={
        <>
          <Who>{t("plan")}</Who>
          <span className="tabular-nums">{t("planCount", { done, total: item.entries.length })}</span>
        </>
      }
    >
      <ul className="flex flex-col gap-1 text-sm" data-testid="trace-plan">
        {item.entries.map((entry, index) => {
          const Icon = entry.status === "completed" ? CircleCheck : entry.status === "in_progress" ? CircleDot : Circle;
          return (
            <li key={index} className="flex min-w-0 items-start gap-2" data-status={entry.status}>
              <Icon
                className={cn(
                  "mt-0.5 size-4 shrink-0",
                  entry.status === "completed" ? "text-success" : entry.status === "in_progress" ? "text-running" : "text-fg-subtle",
                )}
                aria-hidden="true"
              />
              <span className="sr-only">{t(`planStatus.${entry.status}`)}: </span>
              <span className={cn("min-w-0 [overflow-wrap:anywhere]", entry.status === "completed" && "text-muted-foreground")}>
                <LinkedText text={entry.content} />
              </span>
            </li>
          );
        })}
      </ul>
    </Row>
  );
}

export function AskRow({ item, context }: { item: Extract<TraceItem, { type: "ask" }>; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace");
  const mine = context.viewer !== null && context.viewer === context.owner;
  const onPage = context.openDecisions.has(item.decisionId);
  const link = "shrink-0 font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:inline-flex max-md:min-h-11 max-md:items-center";
  return (
    <Row
      icon={MessageSquare}
      look="ask"
      at={item.at}
      head={
        <>
          <Who>{mine ? t("askedYou") : t("asked", { login: context.owner })}</Who>
          <span className="min-w-0 text-foreground [overflow-wrap:anywhere]" data-testid="trace-ask-question">
            <LinkedText text={item.question} />
          </span>
          {onPage || context.showDecision ? (
            <a
              href={`#${decisionAnchor(item.decisionId)}`}
              className={link}
              onClick={(event) => {
                if (!context.showDecision) return;
                event.preventDefault();
                context.showDecision(item.decisionId);
              }}
              data-testid="trace-ask-link"
            >
              {mine && onPage ? t("answer") : t("viewDecision")}
            </a>
          ) : (
            <Link href={decisionHref(item.decisionId)} className={link} data-testid="trace-ask-link">
              {t("inInbox")}
            </Link>
          )}
        </>
      }
    />
  );
}

export function UserRow({ item, context }: { item: Extract<TraceItem, { type: "user" }>; context: TraceContext }) {
  const t = useTranslations("runs.detail.trace");
  const tFiles = useTranslations("attachments");
  const you = item.from !== null && item.from === context.viewer;
  const files = context.project ? runFiles(eventFiles(item.files), context.project, context.runId, item.at) : [];
  const who = you ? t("you") : (item.from ?? t("someone"));
  return (
    <Row
      icon={User}
      look="user"
      at={item.at}
      head={
        <Who>
          {item.decisionId !== null
            ? you
              ? t("youAnswered", { id: item.decisionId })
              : t("answered", { login: who, id: item.decisionId })
            : who}
        </Who>
      }
    >
      {item.text || !files.length ? (
        <p className="text-sm leading-[21px] break-words whitespace-pre-wrap text-foreground">
          <LinkedText text={item.text} />
        </p>
      ) : null}
      <OutputStrip outputs={files} label={tFiles("files")} className="mt-1.5" testId="trace-user-files" />
    </Row>
  );
}

export function SystemRow({ item }: { item: Extract<TraceItem, { type: "system" }> }) {
  const t = useTranslations("runs.detail.trace");
  const Icon = item.tone === "ok" ? CircleCheck : item.tone === "error" ? CircleX : Info;
  return (
    <Row
      icon={Icon}
      look="system"
      system
      at={item.at}
      head={
        <span className={cn("min-w-0 break-words whitespace-pre-wrap", item.tone === "error" && "text-danger", item.tone === "ok" && "text-success")}>
          <LinkedText text={item.text} />
        </span>
      }
    >
      <RecordedFiles seq={item.seq} />
      {item.output ? (
        <details className="group/out min-w-0 overflow-hidden rounded-sm border" data-testid="trace-system-output">
          <summary className="flex h-8 cursor-pointer list-none items-center gap-2 px-3 text-xs text-muted-foreground select-none hover:bg-accent max-md:h-11 [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 transition-transform duration-[var(--transition-duration-base)] group-open/out:rotate-90" aria-hidden="true" />
            {t("systemOutput")}
          </summary>
          <TermBlock text={item.output} testId="trace-system-output-text" />
        </details>
      ) : null}
    </Row>
  );
}

export function MoveRow({ item, context }: { item: Extract<TraceItem, { type: "move" }>; context: TraceContext }) {
  const bad = item.move.to === "failed" || item.move.to === "lost";
  const good = item.move.to === "done";
  return (
    <Row
      icon={bad ? CircleX : good ? CircleCheck : CircleDot}
      look="system"
      system
      at={item.at}
      head={
        <span className={cn("min-w-0 [overflow-wrap:anywhere]", bad && "text-danger", good && "text-success")}>
          <LinkedText text={context.describe(item.move)} />
        </span>
      }
    />
  );
}
