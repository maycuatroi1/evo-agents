"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus, Search, UserPlus, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { chatPeopleQuery } from "@/lib/chat-queries";
import { type ChatCandidate, LOGIN } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { PersonAvatar } from "./faces";

/** Rows the list draws at most; a search narrows the rest. */
export const SHOW_AT_MOST = 100;

const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();
/** The hub's words as a sentence of their own: the first letter upper case. */
const sentence = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** The login a search names when it is one (an `@` before it left out); null when it is not. */
export function typedLogin(query: string): string | null {
  const value = query.trim().replace(/^@/, "");
  return LOGIN.test(value) ? value : null;
}

/** The candidates whose login holds `query`, ignoring case, in the hub's order; all of them for a blank search. */
export function matching(people: readonly ChatCandidate[], query: string): ChatCandidate[] {
  const wanted = query.trim().replace(/^@/, "").toLowerCase();
  return wanted ? people.filter((person) => person.login.toLowerCase().includes(wanted)) : [...people];
}

/**
 * Search and pick people for a chat (web/DESIGN.md, Chats): a search field over the people the visitor shares a
 * project or a chat with (`GET /v1/chats/people`), each with their face and login. One who could not be in a chat with
 * `agents` says why, in the hub's words, and cannot be picked. A login typed that matches nobody listed is offered as it
 * is, for someone else who signed in to the hub; the hub says when nobody did. In New chat (`mode` pick) each row is a
 * checkbox of `picked`; in a chat's Members (`mode` add) a button that adds the person at once. Enter in the field
 * takes the login typed when it is listed, else the one row the search leaves, else the login typed as it is.
 */
export function PeoplePicker({
  id,
  mode,
  agents,
  exclude = [],
  picked = [],
  full = false,
  busy = false,
  describedBy,
  onPick,
}: {
  id: string;
  mode: "pick" | "add";
  agents: readonly string[];
  exclude?: readonly string[];
  picked?: readonly string[];
  full?: boolean;
  busy?: boolean;
  describedBy?: string;
  onPick: (login: string, on: boolean) => void;
}) {
  const t = useTranslations("chats.picker");
  const people = useQuery(chatPeopleQuery(browserApi, agents));
  const [query, setQuery] = useState("");
  const isPicked = (login: string) => picked.some((item) => same(item, login));
  const listed = (people.data ?? []).filter((person) => !exclude.some((login) => same(login, person.login)));
  const found = matching(listed, query);
  const shown = found.slice(0, SHOW_AT_MOST);
  const typed = typedLogin(query);
  const offerTyped = typed !== null && ![...listed.map((person) => person.login), ...exclude, ...picked].some((login) => same(login, typed));
  const takes = (person: ChatCandidate) => !busy && person.why === null && (isPicked(person.login) ? mode === "pick" : !full);

  const pick = (login: string) => {
    onPick(login, !isPicked(login));
    setQuery("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const open = found.filter((person) => takes(person) && !isPicked(person.login));
    const exact = found.find((person) => typed !== null && same(person.login, typed));
    if (exact && takes(exact) && !isPicked(exact.login)) pick(exact.login);
    else if (open.length === 1) pick(open[0].login);
    else if (offerTyped && !full && !busy) pick(typed);
  };

  return (
    <div className="flex min-w-0 flex-col overflow-hidden rounded-md border" data-testid="people-picker" data-mode={mode}>
      <div className="flex h-10 items-center gap-2 border-b px-3 text-fg-subtle max-md:h-11">
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <input
          id={id}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
          autoComplete="off"
          spellCheck={false}
          placeholder={t("searchPlaceholder")}
          aria-controls={`${id}-list`}
          aria-describedby={describedBy}
          className="h-full min-w-0 flex-1 bg-transparent text-base text-foreground outline-hidden placeholder:text-fg-subtle md:text-sm [&::-webkit-search-cancel-button]:hidden"
          data-testid="people-search"
        />
        {query ? (
          <button
            type="button"
            onClick={() => setQuery("")}
            aria-label={t("clear")}
            className="-mr-1.5 grid size-7 shrink-0 cursor-pointer place-items-center rounded-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:size-9"
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {people.isPending ? (
        <div className="flex flex-col gap-2 p-3" role="status" aria-label={t("loading")}>
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-6 w-1/2" />
        </div>
      ) : (
        <ul id={`${id}-list`} aria-label={t("list")} aria-busy={people.isFetching || undefined} className="flex max-h-56 flex-col overflow-y-auto" data-testid="people-list">
          {shown.map((person) => {
            const checked = mode === "pick" && isPicked(person.login);
            const can = takes(person) || checked;
            const body = (
              <>
                <PersonAvatar login={person.login} src={person.avatar_url} size="sm" className={cn(!can && "opacity-50")} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className={cn("truncate text-sm font-medium", !can && "text-muted-foreground")}>{person.login}</span>
                  {person.why ? (
                    <span className={cn("text-xs text-pretty", checked ? "font-medium text-danger" : "text-muted-foreground")} data-testid="people-option-why">
                      {checked ? t("cannotJoin", { why: person.why }) : sentence(person.why)}
                    </span>
                  ) : null}
                </span>
              </>
            );
            const row = cn("flex min-h-11 items-center gap-3 px-3 py-1.5 transition-colors", can ? "cursor-pointer hover:bg-accent" : "cursor-not-allowed");
            return (
              <li key={person.login} className="border-b last:border-b-0" data-testid="people-option" data-login={person.login} data-blocked={person.why ? "" : undefined}>
                {mode === "pick" ? (
                  <label className={cn(row, checked && "bg-surface-selected")}>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!can}
                      onChange={() => pick(person.login)}
                      className="size-4 shrink-0 cursor-pointer accent-primary disabled:cursor-not-allowed"
                      data-testid="people-option-check"
                    />
                    {body}
                  </label>
                ) : (
                  <button
                    type="button"
                    disabled={!can}
                    onClick={() => pick(person.login)}
                    aria-label={person.why ? `${t("add", { login: person.login })}: ${person.why}` : t("add", { login: person.login })}
                    className={cn(row, "w-full text-left focus-visible:bg-accent focus-visible:outline-hidden")}
                    data-testid="people-option-add"
                  >
                    {body}
                    <UserPlus className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  </button>
                )}
              </li>
            );
          })}
          {offerTyped ? (
            <li className="border-b last:border-b-0">
              <button
                type="button"
                disabled={full || busy}
                onClick={() => pick(typed)}
                className="flex min-h-11 w-full cursor-pointer items-center gap-3 px-3 py-1.5 text-left transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:outline-hidden disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="people-option-typed"
                data-login={typed}
              >
                <span className="grid size-6 shrink-0 place-items-center rounded-full border border-dashed text-muted-foreground" aria-hidden="true">
                  <Plus className="size-3.5" />
                </span>
                <span className="min-w-0 flex-1 text-sm">{t("addTyped", { login: typed })}</span>
              </button>
            </li>
          ) : null}
          {shown.length === 0 && !offerTyped ? (
            <li className="px-3 py-2.5 text-xs text-pretty text-muted-foreground" data-testid="people-empty">
              {people.isError ? t("failed") : query.trim() ? t("noMatch", { query: query.trim() }) : listed.length === 0 && (people.data?.length ?? 0) > 0 ? t("allIn") : t("empty")}
            </li>
          ) : null}
          {found.length > shown.length ? (
            <li className="px-3 py-2 text-xs text-fg-subtle" data-testid="people-more">
              {t("more", { count: found.length - shown.length })}
            </li>
          ) : null}
        </ul>
      )}
    </div>
  );
}
