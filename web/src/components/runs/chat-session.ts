import type { ApiSource } from "@/lib/queries";

import { type Run, runsQuery } from "./queries";

/**
 * The turns of a chat session (docs/hub.md, Group chats): each run of an agent in a thread for one person is a turn of
 * one session, titled by the message that woke it; the first run's title is the session's. The API says of each chat
 * run its session's first run (`session_run_id`), its turn (`session_turn`, 1 for the first), the turns so far
 * (`session_turns`) and the session's title; the Runs page lists a session once, as its latest turn (`group=session`).
 */

/** The most turns of one session the API lists at once (`MAX_LIST`). */
export const SESSION_TURNS = 200;

/** Whether `run` is a turn of a chat session: a chat run the API names the session of. */
export function isSessionTurn(run: Run): boolean {
  return run.kind === "chat" && typeof run.session_run_id === "number";
}

/** Whether `run` is a turn of a chat session of more than one turn, which then has turns to list. */
export function hasOtherTurns(run: Run): boolean {
  return isSessionTurn(run) && (run.session_turns ?? 1) > 1;
}

/**
 * What a turn's title says that its session's title does not: the text after "@agent in <chat>: " when both titles
 * start with it (the hub titles each turn so, `chat_runs.turn_title`), else the whole title. A chat title with ": " in
 * it ends the shared start early, which leaves more of the turn's text, never less.
 */
export function turnText(title: string | null | undefined, sessionTitle: string | null | undefined, agent: string | null | undefined): string | null {
  if (!title) return null;
  const lead = agent ? `@${agent} in ` : null;
  if (!lead || !sessionTitle?.startsWith(lead)) return title;
  const cut = sessionTitle.indexOf(": ", lead.length);
  if (cut < 0) return title;
  const shared = sessionTitle.slice(0, cut + 2);
  return title.startsWith(shared) && title.length > shared.length ? title.slice(shared.length) : title;
}

/** The turns of a session as the API lists them (newest first), in turn order: the conversation as it went. */
export function turnsInOrder(runs: readonly Run[]): Run[] {
  return [...runs].sort((a, b) => (a.session_turn ?? 0) - (b.session_turn ?? 0) || a.id - b.id);
}

/** The turns of the session whose first run is `sessionRunId`, read again every 5 seconds while one of them is active. */
export const sessionTurnsQuery = (api: ApiSource, project: string, sessionRunId: number) =>
  runsQuery(api, project, { sessionRunId, limit: SESSION_TURNS });
