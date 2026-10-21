"use client";

import { Bot } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOptGroup, NativeSelectOption } from "@/components/ui/native-select";
import { type Chat, MAX_AGENTS, MAX_PEOPLE } from "@/lib/chats";

import { PeoplePicker } from "./people-picker";
import { useAddableAgents } from "./use-addable-agents";

const FIELD = "flex flex-col gap-1.5";
const ROW = "flex flex-wrap items-start gap-2";

/** Add agent: a select of the agents of the projects the visitor writes to that are not in the chat, by project. */
export function AddAgentForm({ chat, pending, onAdd }: { chat: Chat; pending: boolean; onAdd: (handle: string) => void }) {
  const t = useTranslations("chats.members");
  const ids = useId();
  const addable = useAddableAgents();
  const [handle, setHandle] = useState("");
  const inChat = new Set(chat.agents.map((agent) => agent.handle));
  const agents = addable.status === "ready" ? addable.agents.filter((agent) => !inChat.has(agent.handle)) : [];
  const projects = [...new Set(agents.map((agent) => agent.project))];
  const full = chat.agents.length >= MAX_AGENTS;
  const picked = agents.some((agent) => agent.handle === handle) ? handle : "";
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (picked) onAdd(picked);
  };
  return (
    <form onSubmit={submit} className={FIELD} data-testid="chat-add-agent">
      <label htmlFor={`${ids}-agent`} className="text-[13px] font-medium">
        {t("addAgent")}
      </label>
      <div className={ROW}>
        <NativeSelect
          id={`${ids}-agent`}
          value={picked}
          onChange={(event) => setHandle(event.target.value)}
          disabled={full || addable.status === "loading" || agents.length === 0}
          className="min-w-0 flex-1"
          aria-describedby={`${ids}-agent-hint`}
          data-testid="chat-add-agent-select"
        >
          <NativeSelectOption value="">{addable.status === "loading" ? t("loadingAgents") : t("pickAgent")}</NativeSelectOption>
          {projects.map((project) => (
            <NativeSelectOptGroup key={project} label={project}>
              {agents
                .filter((agent) => agent.project === project)
                .map((agent) => (
                  <NativeSelectOption key={agent.handle} value={agent.handle}>
                    {agent.is_default ? t("defaultAgent", { name: agent.name, handle: agent.handle }) : t("agentOption", { name: agent.name, handle: agent.handle })}
                  </NativeSelectOption>
                ))}
            </NativeSelectOptGroup>
          ))}
        </NativeSelect>
        <Button type="submit" variant="outline" disabled={!picked || full} busy={pending} data-testid="chat-add-agent-submit">
          <Bot aria-hidden="true" />
          {pending ? t("adding") : t("add")}
        </Button>
      </div>
      <p id={`${ids}-agent-hint`} className="text-xs text-pretty text-fg-subtle">
        {full
          ? t("agentsFull", { max: MAX_AGENTS })
          : addable.status === "ready" && addable.failed.length
            ? t("agentsFailed", { projects: addable.failed.join(", ") })
            : addable.status === "ready" && agents.length === 0
              ? t("noAgents")
              : t("agentHint")}
      </p>
    </form>
  );
}

/** Add person: the people the visitor may add, searched, each added at once (`PeoplePicker`); who is in it left out. */
export function AddPersonForm({ chat, pending, onAdd }: { chat: Chat; pending: boolean; onAdd: (login: string) => void }) {
  const t = useTranslations("chats.members");
  const ids = useId();
  const full = chat.people.length >= MAX_PEOPLE;
  return (
    <div className={FIELD} data-testid="chat-add-person">
      <label htmlFor={`${ids}-people`} className="text-[13px] font-medium">
        {t("addPerson")}
      </label>
      <PeoplePicker
        id={`${ids}-people`}
        mode="add"
        agents={chat.agents.map((agent) => agent.handle)}
        exclude={chat.people.map((person) => person.login)}
        full={full}
        busy={pending}
        describedBy={`${ids}-people-hint`}
        onPick={(login) => onAdd(login)}
      />
      <p id={`${ids}-people-hint`} className="text-xs text-pretty text-fg-subtle" data-testid="chat-add-person-hint">
        {full ? t("peopleFull", { max: MAX_PEOPLE }) : t("personHint")}
      </p>
    </div>
  );
}
