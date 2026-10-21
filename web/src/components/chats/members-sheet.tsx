"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderKanban, Lock, LogOut, Star, UserRoundMinus, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { Tag } from "@/components/data/identifier";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { browserApi } from "@/lib/api/browser";
import { addChatAgent, addChatPerson, chatKeys, removeChatAgent, removeChatPerson } from "@/lib/chat-queries";
import { type Chat, chatsHref, MAX_AGENTS, MAX_PEOPLE } from "@/lib/chats";

import { PersonAvatar } from "./faces";
import { useChatFailure } from "./hooks";
import { AddAgentForm, AddPersonForm } from "./member-forms";

type Write =
  | { kind: "addAgent"; handle: string }
  | { kind: "removeAgent"; handle: string }
  | { kind: "addPerson"; login: string }
  | { kind: "removePerson"; login: string };

const ROW = "flex min-h-11 items-center gap-3 rounded-sm px-2 py-1.5";

function Section({ title, count, children, testId }: { title: string; count: string; children: ReactNode; testId: string }) {
  return (
    <section className="flex flex-col gap-2" aria-label={title} data-testid={testId}>
      <h3 className="flex items-baseline justify-between gap-2 text-[15px] leading-[22px] font-semibold">
        {title}
        <span className="text-xs font-normal text-fg-subtle tabular-nums">{count}</span>
      </h3>
      {children}
    </section>
  );
}

/**
 * The chat's members in a sheet (web/DESIGN.md, Chats): its agents (any member adds one of a project they write to,
 * or takes one out) and its people (any member adds one; the creator takes others out; each may leave). The hub
 * checks every change again: a refusal shows at the top of the sheet with the hub's words, a change made is a toast,
 * and the chat's stream tells the other members. A direct chat's sheet is its Details: its agent and its person, for
 * good (nobody is added, taken out or leaves; a note says to start a group chat for that).
 */
export function MembersSheet({ chat, me, open, onOpenChange }: { chat: Chat; me: string | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("chats.members");
  const queryClient = useQueryClient();
  const router = useRouter();
  const failure = useChatFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  const [leaving, setLeaving] = useState(false);
  const creator = !!me && chat.created_by.toLowerCase() === me.toLowerCase();
  const direct = chat.kind === "direct";

  const write = useMutation({
    mutationFn: async (change: Write): Promise<Chat | null> => {
      const api = browserApi();
      if (change.kind === "addAgent") return addChatAgent(api, chat.id, change.handle);
      if (change.kind === "addPerson") return addChatPerson(api, chat.id, change.login);
      if (change.kind === "removeAgent") await removeChatAgent(api, chat.id, change.handle);
      else await removeChatPerson(api, chat.id, change.login);
      return null;
    },
    onMutate: () => setError(null),
    onError: (cause) => setError(failure(cause)),
    onSuccess: (updated, change) => {
      if (updated) queryClient.setQueryData(chatKeys.one(chat.id), updated);
      void queryClient.invalidateQueries({ queryKey: chatKeys.one(chat.id) });
      void queryClient.invalidateQueries({ queryKey: chatKeys.list });
      const leftSelf = change.kind === "removePerson" && change.login.toLowerCase() === me?.toLowerCase();
      if (leftSelf) {
        setLeaving(false);
        notify({ tone: "success", text: t("left", { title: chat.title }) });
        router.push(chatsHref());
        return;
      }
      const name = "handle" in change ? `@${change.handle}` : "login" in change ? change.login : "";
      notify({ tone: "success", text: t(`done.${change.kind}`, { name }) });
    },
  });
  const pendingOf = (kind: Write["kind"]) => write.isPending && write.variables?.kind === kind;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" showCloseButton={false} className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-md" data-testid="chat-members">
        <SheetHeader className="flex-row items-start gap-3 border-b p-4">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <SheetTitle className="text-[15px] leading-[22px] font-semibold">{direct ? t("detailsTitle") : t("title")}</SheetTitle>
            <SheetDescription className="text-[13px] leading-[18px]">
              {direct ? t("directDescription", { name: chat.agents[0]?.name ?? chat.title }) : t("description", { title: chat.title })}
            </SheetDescription>
          </div>
          <Button type="button" variant="ghost" size="icon-sm" aria-label={t("close")} onClick={() => onOpenChange(false)}>
            <X aria-hidden="true" />
          </Button>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto p-4">
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          {direct ? (
            <p className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-3 py-2.5 text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="chat-direct-note">
              <Lock className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              {t("directNote")}
            </p>
          ) : null}
          <Section title={t("agents")} count={direct ? "" : t("countOf", { count: chat.agents.length, max: MAX_AGENTS })} testId="chat-members-agents">
            {chat.agents.length ? (
              <ul className="flex flex-col gap-0.5">
                {chat.agents.map((agent) => (
                  <li key={agent.handle} className={ROW} data-testid="chat-member-agent" data-handle={agent.handle}>
                    <AgentAvatar agent={agent} />
                    <div className="flex min-w-0 flex-1 flex-col">
                      <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                        <span className="truncate text-sm font-medium">{agent.name}</span>
                        {agent.is_default ? (
                          <Tag>
                            <Star aria-hidden="true" />
                            {t("default")}
                          </Tag>
                        ) : null}
                        {agent.deleted ? <Tag className="text-danger">{t("deleted")}</Tag> : null}
                      </span>
                      <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                        <span className="truncate font-mono">@{agent.handle}</span>
                        <FolderKanban className="size-3 shrink-0" aria-hidden="true" />
                        <span className="truncate">{agent.project}</span>
                      </span>
                    </div>
                    {direct ? null : (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={t("removeAgent", { handle: agent.handle })}
                        title={t("removeAgent", { handle: agent.handle })}
                        busy={write.isPending && write.variables?.kind === "removeAgent" && write.variables.handle === agent.handle}
                        onClick={() => write.mutate({ kind: "removeAgent", handle: agent.handle })}
                        data-testid="chat-remove-agent"
                      >
                        <X aria-hidden="true" />
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-muted-foreground">{t("noAgentsYet")}</p>
            )}
            {direct ? null : <AddAgentForm chat={chat} pending={pendingOf("addAgent")} onAdd={(handle) => write.mutate({ kind: "addAgent", handle })} />}
          </Section>
          <Section title={t("people")} count={direct ? "" : t("countOf", { count: chat.people.length, max: MAX_PEOPLE })} testId="chat-members-people">
            <ul className="flex flex-col gap-0.5">
              {chat.people.map((person) => {
                const self = person.login.toLowerCase() === me?.toLowerCase();
                const madeIt = person.login.toLowerCase() === chat.created_by.toLowerCase();
                return (
                  <li key={person.login} className={ROW} data-testid="chat-member-person" data-login={person.login}>
                    <PersonAvatar login={person.login} src={person.avatar_url} />
                    <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
                      <span className="truncate text-sm font-medium">{person.login}</span>
                      {self ? <span className="text-xs text-fg-subtle">{t("you")}</span> : null}
                      {madeIt ? <Tag>{t("creator")}</Tag> : null}
                    </span>
                    {direct ? null : self ? (
                      <Button type="button" variant="quiet-danger" size="sm" onClick={() => setLeaving(true)} data-testid="chat-leave">
                        <LogOut aria-hidden="true" />
                        {t("leave")}
                      </Button>
                    ) : creator ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={t("removePerson", { login: person.login })}
                        title={t("removePerson", { login: person.login })}
                        busy={write.isPending && write.variables?.kind === "removePerson" && write.variables.login === person.login}
                        onClick={() => write.mutate({ kind: "removePerson", login: person.login })}
                        data-testid="chat-remove-person"
                      >
                        <UserRoundMinus aria-hidden="true" />
                      </Button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
            {direct ? null : <AddPersonForm chat={chat} pending={pendingOf("addPerson")} onAdd={(login) => write.mutate({ kind: "addPerson", login })} />}
          </Section>
          {chat.restricted ? (
            <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="chat-restricted">
              {t("restricted")}
            </p>
          ) : null}
        </div>
      </SheetContent>
      <ConfirmAction
        open={leaving}
        onOpenChange={setLeaving}
        title={t("leaveTitle", { title: chat.title })}
        description={t("leaveBody")}
        confirmLabel={t("leave")}
        pendingLabel={t("leaving")}
        cancelLabel={t("cancel")}
        pending={pendingOf("removePerson")}
        error={leaving ? error : null}
        onConfirm={() => me && write.mutate({ kind: "removePerson", login: me })}
        testId="chat-leave-confirm"
      />
    </Sheet>
  );
}
