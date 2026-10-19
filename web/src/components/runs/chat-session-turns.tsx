"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect } from "react";

import { NAME_LINK, RunRef } from "@/components/data/identifier";
import { StatusBadge } from "@/components/status/status-badge";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { sessionTurnsQuery, turnsInOrder, turnText } from "./chat-session";
import { type Run, runHref } from "./queries";

/**
 * The turns of a chat session (`chat-session.ts`) where the pages show them: the disclosure of a session's row on the
 * Runs page and the turns it opens, and the head of a turn's own page, with its session's title and the other turns.
 */

/**
 * The turns of the session of `run` (one of them), oldest first, read again while one is active, and at once when the
 * run says the session has more than the list holds (a turn queued since).
 */
function useSessionTurns(run: Run, enabled = true) {
  const session = run.session_run_id ?? run.id;
  const query = useQuery({ ...sessionTurnsQuery(browserApi, run.project, session), enabled });
  const { data, refetch } = query;
  const behind = data !== undefined && data.total < (run.session_turns ?? 1);
  useEffect(() => {
    if (behind) void refetch();
  }, [behind, refetch]);
  return { ...query, turns: data ? turnsInOrder(data.runs) : [] };
}

/** The disclosure of a session's row: "3 turns", its chevron turned down and the chip pressed while the turns show. */
export function TurnsToggle({ run, open, controls, onToggle }: { run: Run; open: boolean; controls: string; onToggle: () => void }) {
  const t = useTranslations("runs.session");
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={open ? controls : undefined}
      onClick={onToggle}
      // 24 px to hit and to point at, 20 px of the line: the row keeps its height.
      className="-my-0.5 inline-flex h-6 shrink-0 cursor-pointer items-center gap-0.5 rounded-sm border border-border-strong bg-card pr-1.5 pl-1 text-xs leading-4 font-medium text-muted-foreground tabular-nums transition-colors hover:border-input hover:text-foreground aria-expanded:border-brand/40 aria-expanded:bg-surface-selected aria-expanded:text-foreground"
      data-testid="runs-session-toggle"
      data-session={run.session_run_id ?? undefined}
    >
      <ChevronRight className={cn("size-3.5 transition-transform duration-base", open && "rotate-90")} aria-hidden="true" />
      {t("turns", { count: run.session_turns ?? 1 })}
    </button>
  );
}

/** One turn in a session's list: "Turn 2", what its message said as the link to its page, its state, number and start. */
function TurnRow({ turn, session }: { turn: Run; session: Run }) {
  const t = useTranslations("runs.session");
  const format = useFormatter();
  const at = turn.started_at ?? turn.queued_at;
  const text = turnText(turn.title, session.session_title, session.profile) ?? t("untitled");
  return (
    <li className="flex min-h-9 min-w-0 items-center gap-3 pl-3" data-testid="session-turn" data-turn={turn.session_turn ?? undefined} data-turn-run-id={turn.id}>
      <span className="w-14 shrink-0 text-xs text-fg-subtle tabular-nums">{t("turn", { turn: turn.session_turn ?? 1 })}</span>
      {/* The link fills the line's height, so the whole line of words is the target. */}
      <Link
        href={runHref(turn.project, turn.id)}
        className={cn(NAME_LINK, "flex min-w-0 flex-1 items-center self-stretch rounded-xs text-[13px] leading-5")}
        title={turn.title ?? undefined}
        data-testid="session-turn-link"
      >
        <span className="truncate">{text}</span>
      </Link>
      <StatusBadge kind="run" status={turn.state} />
      <RunRef id={turn.id} className="hidden sm:inline" />
      <time dateTime={at} className="hidden w-32 shrink-0 text-right text-xs text-fg-subtle tabular-nums lg:inline">
        {format.dateTime(new Date(at), { dateStyle: "short", timeStyle: "short" })}
      </time>
    </li>
  );
}

/**
 * The turns a session's row opened on the Runs page, oldest first, the conversation as it went, along a hairline under
 * the row; while they are read, as many grey lines as there are turns (four at most); when they cannot be, why, and
 * Try again.
 */
export function SessionTurns({ run, id }: { run: Run; id: string }) {
  const t = useTranslations("runs.session");
  const { turns, data, isPending, isError, refetch } = useSessionTurns(run);
  const label = t("turnsLabel", { title: run.session_title ?? `#${run.session_run_id}` });
  if (isPending) {
    return (
      <div id={id} role="status" aria-label={t("loading")} className="ml-1.5 flex flex-col border-l border-border-strong" data-testid="session-turns-loading">
        {Array.from({ length: Math.min(run.session_turns ?? 1, 4) }, (_, index) => (
          <span key={index} className="flex h-9 items-center pl-3">
            <span className="block h-3.5 w-full max-w-md animate-pulse rounded-md bg-muted" />
          </span>
        ))}
      </div>
    );
  }
  if (isError) {
    return (
      <div id={id} role="alert" className="ml-1.5 flex min-h-9 flex-wrap items-center gap-3 border-l border-border-strong pl-3 text-[13px] text-danger" data-testid="session-turns-error">
        {t("error")}
        <button type="button" onClick={() => void refetch()} className="inline-flex h-6 cursor-pointer items-center gap-1 rounded-sm px-1.5 text-xs font-medium text-foreground transition-colors hover:bg-surface-sunken">
          <RotateCcw className="size-3.5" aria-hidden="true" />
          {t("retry")}
        </button>
      </div>
    );
  }
  return (
    <div id={id} className="flex flex-col gap-1">
      <ol aria-label={label} className="ml-1.5 flex flex-col border-l border-border-strong" data-testid="session-turns">
        {turns.map((turn) => (
          <TurnRow key={turn.id} turn={turn} session={run} />
        ))}
      </ol>
      {data.total > turns.length ? <p className="pl-4.5 text-xs text-fg-subtle">{t("more", { shown: turns.length, total: data.total })}</p> : null}
    </div>
  );
}

/** A link to one turn of the session on a turn's page: its number, this one pressed; its words in its name and tooltip. */
function TurnChip({ turn, run }: { turn: Run; run: Run }) {
  const t = useTranslations("runs.session");
  const current = turn.id === run.id;
  const n = turn.session_turn ?? 1;
  const name = t("turnLink", { turn: n, title: turnText(turn.title, run.session_title, run.profile) ?? t("untitled") });
  return (
    <li>
      <Link
        href={runHref(turn.project, turn.id)}
        aria-label={name}
        aria-current={current ? "page" : undefined}
        title={name}
        className={cn(
          "inline-flex h-6 min-w-6 items-center justify-center rounded-sm border px-1.5 text-xs leading-4 tabular-nums transition-colors max-md:h-11 max-md:min-w-11",
          current ? "border-brand/40 bg-surface-selected font-medium text-foreground" : "border-border-strong text-muted-foreground hover:border-input hover:text-foreground",
        )}
        data-testid="run-turn-link"
        data-turn={n}
      >
        {n}
      </Link>
    </li>
  );
}

/**
 * The head of a chat turn's page, over its one line: the session's title; and when the session has more than this
 * turn, "Turn 2 of 3", what this turn's message said when it is not the first, and a link to each turn by its number.
 * Under 768 px the title and the turn's words take two lines each at most, the words under the links of 44 px.
 */
export function ChatSessionHead({ run }: { run: Run }) {
  const t = useTranslations("runs.session");
  const count = run.session_turns ?? 1;
  const many = count > 1;
  const { turns, isPending, isError } = useSessionTurns(run, many);
  const own = run.session_turn !== 1 ? turnText(run.title, run.session_title, run.profile) : null;
  const title = run.session_title ?? run.title ?? t("untitled");
  return (
    <div className="flex min-w-0 basis-full flex-col gap-1" data-testid="run-session" data-session={run.session_run_id ?? undefined}>
      <p className="min-w-0 truncate text-[15px] leading-[22px] font-semibold text-foreground max-md:line-clamp-2 max-md:whitespace-normal" title={title} data-testid="run-session-title">
        {title}
      </p>
      {many ? (
        <nav aria-label={t("navLabel")} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-[13px] leading-5 text-muted-foreground">
          <span className="shrink-0 font-medium text-foreground tabular-nums" data-testid="run-turn">
            {t("turnOf", { turn: run.session_turn ?? 1, turns: count })}
          </span>
          {own ? (
            <span
              className="min-w-0 flex-1 truncate max-md:order-last max-md:basis-full max-md:line-clamp-2 max-md:whitespace-normal"
              title={run.title ?? undefined}
              data-testid="run-turn-text"
            >
              {own}
            </span>
          ) : null}
          {isPending ? (
            <span className="ml-auto block h-6 w-24 animate-pulse rounded-sm bg-muted max-md:h-11" aria-hidden="true" />
          ) : isError ? (
            <span className="ml-auto text-xs text-danger" role="alert">
              {t("navError")}
            </span>
          ) : (
            <ol className="ml-auto flex flex-wrap items-center gap-1" data-testid="run-turns">
              {turns.map((turn) => (
                <TurnChip key={turn.id} turn={turn} run={run} />
              ))}
            </ol>
          )}
        </nav>
      ) : null}
    </div>
  );
}
