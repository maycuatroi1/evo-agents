"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot, UserPlus } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOptGroup, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { chatsQuery } from "@/lib/chat-queries";
import { type Chat, knownPeople, LOGIN, MAX_AGENTS, MAX_BUDGET_USD, MAX_PEOPLE } from "@/lib/chats";

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

/** Add person: a login, with the people of the visitor's other chats suggested. */
export function AddPersonForm({ chat, me, pending, onAdd }: { chat: Chat; me: string | null; pending: boolean; onAdd: (login: string) => void }) {
  const t = useTranslations("chats.members");
  const ids = useId();
  const list = useQuery(chatsQuery(browserApi));
  const [login, setLogin] = useState("");
  const [problem, setProblem] = useState(false);
  const suggestions = knownPeople(list.data ?? [], me, chat.people.map((person) => person.login));
  const full = chat.people.length >= MAX_PEOPLE;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = login.trim().replace(/^@/, "");
    if (!LOGIN.test(value)) return setProblem(true);
    setProblem(false);
    onAdd(value);
    setLogin("");
  };
  return (
    <form onSubmit={submit} noValidate className={FIELD} data-testid="chat-add-person">
      <label htmlFor={`${ids}-login`} className="text-[13px] font-medium">
        {t("addPerson")}
      </label>
      <div className={ROW}>
        <Input
          id={`${ids}-login`}
          value={login}
          onChange={(event) => {
            setLogin(event.target.value);
            setProblem(false);
          }}
          list={`${ids}-people`}
          autoComplete="off"
          spellCheck={false}
          placeholder={t("loginPlaceholder")}
          disabled={full}
          aria-invalid={problem || undefined}
          aria-describedby={`${ids}-login-hint`}
          className="min-w-0 flex-1"
          data-testid="chat-add-person-login"
        />
        <datalist id={`${ids}-people`}>
          {suggestions.map((person) => (
            <option key={person} value={person} />
          ))}
        </datalist>
        <Button type="submit" variant="outline" disabled={full || !login.trim()} busy={pending} data-testid="chat-add-person-submit">
          <UserPlus aria-hidden="true" />
          {pending ? t("adding") : t("add")}
        </Button>
      </div>
      <p id={`${ids}-login-hint`} className={problem ? "text-xs font-medium text-danger" : "text-xs text-pretty text-fg-subtle"} data-testid="chat-add-person-hint">
        {problem ? t("badLogin") : full ? t("peopleFull", { max: MAX_PEOPLE }) : t("personHint")}
      </p>
    </form>
  );
}

/** The chat's budget: what its agents' runs may cost in 24 hours. Its creator changes it; anyone else reads it. */
export function BudgetForm({ chat, creator, pending, onSave }: { chat: Chat; creator: boolean; pending: boolean; onSave: (usd: number) => void }) {
  const t = useTranslations("chats.members");
  const format = useFormatter();
  const ids = useId();
  const [value, setValue] = useState(String(chat.budget_usd));
  const [problem, setProblem] = useState(false);
  const amount = format.number(chat.budget_usd, { style: "currency", currency: "USD" });
  if (!creator) {
    return (
      <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="chat-budget">
        {t("budgetRead", { amount, creator: chat.created_by })}
      </p>
    );
  }
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const usd = Number(value);
    if (!Number.isFinite(usd) || usd <= 0 || usd > MAX_BUDGET_USD) return setProblem(true);
    setProblem(false);
    onSave(usd);
  };
  const changed = Number(value) !== chat.budget_usd;
  return (
    <form onSubmit={submit} noValidate className={FIELD} data-testid="chat-budget-form">
      <label htmlFor={`${ids}-budget`} className="text-[13px] font-medium">
        {t("budgetLabel")}
      </label>
      <div className={ROW}>
        <Input
          id={`${ids}-budget`}
          type="number"
          inputMode="decimal"
          min={0.01}
          max={MAX_BUDGET_USD}
          step={0.5}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            setProblem(false);
          }}
          aria-invalid={problem || undefined}
          aria-describedby={`${ids}-budget-hint`}
          className="w-32 tabular-nums"
          data-testid="chat-budget-input"
        />
        <Button type="submit" variant="outline" disabled={!changed} busy={pending} data-testid="chat-budget-save">
          {pending ? t("saving") : t("save")}
        </Button>
      </div>
      <p id={`${ids}-budget-hint`} className={problem ? "text-xs font-medium text-danger" : "text-xs text-pretty text-fg-subtle"}>
        {problem ? t("badBudget", { max: format.number(MAX_BUDGET_USD) }) : t("budgetHint", { amount })}
      </p>
    </form>
  );
}
