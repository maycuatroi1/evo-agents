"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessagesSquare, Plus, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { chatKeys, chatsQuery, createChat } from "@/lib/chat-queries";
import { type ChatCreate, chatHref, knownPeople, LOGIN, MAX_AGENTS, MAX_PEOPLE, MAX_TITLE_CHARS, titleProblem } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { useChatFailure, useMe } from "./hooks";
import { useAddableAgents } from "./use-addable-agents";

/** A filter shows over the agents once there are more than this many. */
const FILTER_FROM = 8;

/**
 * New chat (web/DESIGN.md, Chats): a title, the agents it starts with (of the projects the visitor writes to, by
 * project) and the people (logins, those of the visitor's other chats suggested). The visitor is in it anyway. The form
 * repeats the hub's limits; the hub's own refusal (a person not signed in, a grant that does not clear the agents'
 * projects) stays in the dialog with its words. Created, the chat opens.
 */
export function NewChatDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [pending, setPending] = useState(false);
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="new-chat-dialog"
      >
        {open ? <NewChatForm onPending={setPending} onClose={() => onOpenChange(false)} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function NewChatForm({ onPending, onClose }: { onPending: (pending: boolean) => void; onClose: () => void }) {
  const t = useTranslations("chats.create");
  const ids = useId();
  const router = useRouter();
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const me = useMe();
  const addable = useAddableAgents();
  const list = useQuery(chatsQuery(browserApi));
  const [title, setTitle] = useState("");
  const [agents, setAgents] = useState<string[]>([]);
  const [people, setPeople] = useState<string[]>([]);
  const [login, setLogin] = useState("");
  const [loginProblem, setLoginProblem] = useState<"bad" | "full" | null>(null);
  const [filter, setFilter] = useState("");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);

  const create = useMutation({
    mutationFn: (body: ChatCreate) => createChat(browserApi(), body),
    onMutate: () => {
      onPending(true);
      setError(null);
    },
    onError: (cause) => setError(failure(cause)),
    onSettled: () => onPending(false),
    onSuccess: (chat) => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
      queryClient.setQueryData(chatKeys.one(chat.id), chat);
      notify({ tone: "success", text: t("created", { title: chat.title }) });
      onClose();
      router.push(chatHref(chat.id));
    },
  });

  const problem = titleProblem(title);
  const choices = addable.status === "ready" ? addable.agents : [];
  const wanted = filter.trim().toLowerCase();
  const shown = wanted ? choices.filter((agent) => agent.handle.includes(wanted) || agent.name.toLowerCase().includes(wanted) || agent.project.includes(wanted)) : choices;
  const projects = [...new Set(shown.map((agent) => agent.project))];
  const suggestions = knownPeople(list.data ?? [], me, people);

  /** The people with the login typed in the field, once it is one; null when it is not (the field says why). */
  const withTyped = (): string[] | null => {
    const value = login.trim().replace(/^@/, "");
    if (!value) return people;
    if (!LOGIN.test(value)) {
      setLoginProblem("bad");
      return null;
    }
    const known = value.toLowerCase() === me?.toLowerCase() || people.some((person) => person.toLowerCase() === value.toLowerCase());
    if (known) return people;
    if (people.length >= MAX_PEOPLE - 1) {
      setLoginProblem("full");
      return null;
    }
    return [...people, value];
  };

  const addPerson = () => {
    const next = withTyped();
    if (next === null) return;
    setLoginProblem(null);
    setPeople(next);
    setLogin("");
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTried(true);
    const everyone = withTyped();
    if (problem || everyone === null || create.isPending) return;
    setPeople(everyone);
    setLogin("");
    create.mutate({ title: title.trim(), agents, people: everyone });
  };

  return (
    <form onSubmit={submit} noValidate className="flex min-h-0 flex-1 flex-col" data-testid="new-chat-form">
      <DialogHeader className="flex-row items-start gap-3 border-b px-4 py-4 sm:px-5">
        <span className="grid size-8 shrink-0 place-items-center rounded-md bg-surface-sunken text-muted-foreground" aria-hidden="true">
          <MessagesSquare className="size-4" />
        </span>
        <div className="flex min-w-0 flex-col gap-1 text-left">
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </div>
      </DialogHeader>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5">
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-title`} className="text-[13px] font-medium">
            {t("titleLabel")}
          </label>
          <Input
            id={`${ids}-title`}
            value={title}
            maxLength={MAX_TITLE_CHARS * 2}
            onChange={(event) => setTitle(event.target.value)}
            placeholder={t("titlePlaceholder")}
            aria-invalid={(tried && problem !== null) || undefined}
            aria-describedby={`${ids}-title-hint`}
            autoFocus
            data-testid="new-chat-title"
          />
          <p id={`${ids}-title-hint`} className={tried && problem ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"} data-testid="new-chat-title-hint">
            {tried && problem ? t(`titleProblems.${problem}`, { max: MAX_TITLE_CHARS }) : t("titleHint")}
          </p>
        </div>

        <fieldset className="flex min-w-0 flex-col gap-1.5" data-testid="new-chat-agents">
          <legend className="mb-1.5 flex w-full items-baseline justify-between gap-2 text-[13px] font-medium">
            {t("agentsLabel")}
            <span className="text-xs font-normal text-fg-subtle tabular-nums" data-testid="new-chat-agents-count">
              {t("picked", { count: agents.length, max: MAX_AGENTS })}
            </span>
          </legend>
          {choices.length > FILTER_FROM ? (
            <Input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("filterAgents")} aria-label={t("filterAgents")} className="mb-1" />
          ) : null}
          {addable.status === "loading" ? (
            <Skeleton className="h-24 w-full" />
          ) : choices.length === 0 ? (
            <p className="rounded-md border border-dashed px-3 py-2 text-xs text-pretty text-muted-foreground">{t("noAgents")}</p>
          ) : (
            <div className="flex max-h-64 flex-col overflow-y-auto rounded-md border">
              {projects.map((project) => (
                <div key={project} role="group" aria-labelledby={`${ids}-project-${project}`}>
                  <p id={`${ids}-project-${project}`} className="border-b bg-surface-sunken px-3 py-1 text-[11px] leading-4 font-semibold tracking-[0.06em] text-muted-foreground uppercase">
                    {project}
                  </p>
                  <ul>
                    {shown
                      .filter((agent) => agent.project === project)
                      .map((agent) => {
                        const checked = agents.includes(agent.handle);
                        const full = !checked && agents.length >= MAX_AGENTS;
                        return (
                          <li key={agent.handle} className="border-b last:border-b-0">
                            <label className={cn("flex min-h-11 cursor-pointer items-center gap-3 px-3 py-1.5 transition-colors hover:bg-accent", checked && "bg-surface-selected", full && "cursor-not-allowed opacity-60")}>
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={full}
                                onChange={(event) => setAgents(event.target.checked ? [...agents, agent.handle] : agents.filter((handle) => handle !== agent.handle))}
                                className="size-4 shrink-0 cursor-pointer accent-primary"
                                data-testid="new-chat-agent"
                                data-handle={agent.handle}
                              />
                              <AgentAvatar agent={agent} size="sm" />
                              <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
                                <span className="text-sm font-medium">{agent.name}</span>
                                <span className="font-mono text-xs text-muted-foreground">@{agent.handle}</span>
                              </span>
                            </label>
                          </li>
                        );
                      })}
                  </ul>
                </div>
              ))}
            </div>
          )}
          <p className="text-xs text-pretty text-fg-subtle">
            {addable.status === "ready" && addable.failed.length ? t("agentsFailed", { projects: addable.failed.join(", ") }) : t("agentsHint")}
          </p>
        </fieldset>

        <div className="flex flex-col gap-1.5" data-testid="new-chat-people">
          <label htmlFor={`${ids}-login`} className="text-[13px] font-medium">
            {t("peopleLabel")}
          </label>
          <div className="flex items-start gap-2">
            <Input
              id={`${ids}-login`}
              value={login}
              list={`${ids}-known`}
              autoComplete="off"
              spellCheck={false}
              placeholder={t("loginPlaceholder")}
              onChange={(event) => {
                setLogin(event.target.value);
                setLoginProblem(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === ",") {
                  event.preventDefault();
                  addPerson();
                }
              }}
              aria-invalid={loginProblem !== null || undefined}
              aria-describedby={`${ids}-login-hint`}
              className="min-w-0 flex-1"
              data-testid="new-chat-login"
            />
            <datalist id={`${ids}-known`}>
              {suggestions.map((person) => (
                <option key={person} value={person} />
              ))}
            </datalist>
            <Button type="button" variant="outline" onClick={addPerson} disabled={!login.trim()} data-testid="new-chat-add-person">
              <Plus aria-hidden="true" />
              {t("addPerson")}
            </Button>
          </div>
          <p id={`${ids}-login-hint`} className={loginProblem ? "text-xs font-medium text-danger" : "text-xs text-pretty text-fg-subtle"}>
            {loginProblem === "bad" ? t("badLogin") : loginProblem === "full" ? t("peopleFull", { max: MAX_PEOPLE }) : t("peopleHint")}
          </p>
          {people.length ? (
            <ul className="flex flex-wrap gap-1.5" aria-label={t("peopleList")} data-testid="new-chat-people-list">
              {people.map((person) => (
                <li key={person} className="inline-flex h-7 items-center gap-1 rounded-xs border bg-card pr-0.5 pl-2 text-[13px] max-md:h-9">
                  {person}
                  <button
                    type="button"
                    aria-label={t("removePerson", { login: person })}
                    onClick={() => setPeople(people.filter((item) => item !== person))}
                    className="grid size-6 cursor-pointer place-items-center rounded-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground max-md:size-8"
                  >
                    <X className="size-3.5" aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col-reverse gap-2 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:justify-end sm:px-5">
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg" aria-disabled={create.isPending || undefined} className="aria-disabled:opacity-50" onClick={(event) => create.isPending && event.preventDefault()}>
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={create.isPending} data-testid="new-chat-submit">
          {create.isPending ? t("creating") : t("submit")}
        </Button>
      </div>
    </form>
  );
}
