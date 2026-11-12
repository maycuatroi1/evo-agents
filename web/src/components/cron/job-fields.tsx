"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useMemo } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { agentsQuery } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";
import { chatsQuery } from "@/lib/chat-queries";
import { plansQuery } from "@/lib/plan-queries";
import { projectQuery } from "@/lib/queries";

import { type CronField, type CronForm, knownTimeZones, MAX_COMMAND, MAX_PROMPT, MAX_TIMEOUT } from "./model";
import { KIND_ICONS, useScheduleText } from "./parts";
import { CRON_KINDS, type CronKind } from "./queries";

/** A field's label, its control, and its help, or the error that replaces the help once the writer tried to save. */
function Field({ id, label, help, error, children }: { id: string; label: string; help?: ReactNode; error?: string | null; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : help ? (
        <p id={`${id}-help`} className="text-xs text-fg-subtle">
          {help}
        </p>
      ) : null}
    </div>
  );
}

/** The aria attributes of a control: its help or error, and whether it is wrong. */
function described(id: string, wrong: boolean) {
  return { "aria-describedby": wrong ? `${id}-error` : `${id}-help`, "aria-invalid": wrong || undefined };
}

/** The project's agents, active plans and repos and the visitor's group chats, the choices of the work fields. */
function useChoices(project: string) {
  const agents = useQuery(agentsQuery(browserApi, project)).data ?? [];
  const plans = useQuery(plansQuery(browserApi, project)).data ?? [];
  const chats = useQuery(chatsQuery(browserApi)).data ?? [];
  const repos = useQuery(projectQuery(browserApi, project)).data?.repos ?? [];
  return {
    agents: agents.filter((agent) => agent.deleted_at === null).map((agent) => agent.handle),
    plans: plans.filter((plan) => plan.area === "active").map((plan) => ({ id: plan.plan_id, title: plan.title })),
    chats: chats.filter((chat) => chat.kind === "group").map((chat) => ({ id: String(chat.id), title: chat.title })),
    repos: repos.map((repo) => repo.name),
  };
}

/**
 * The fields of a cron job, for a new one and an edit: its name (new only, since the name is its address), what it
 * does (agent, plan or command, each with its own fields), and when, as a cron expression read back in words, in an
 * IANA time zone. `wrong` names the fields to mark once the writer tried to save.
 */
export function CronJobFields({
  project,
  form,
  onChange,
  wrong,
  isNew,
}: {
  project: string;
  form: CronForm;
  onChange: (form: CronForm) => void;
  wrong: readonly CronField[];
  isNew: boolean;
}) {
  const t = useTranslations("cron.form");
  const kinds = useTranslations("cron.kind");
  const ids = useId();
  const choices = useChoices(project);
  const zones = useMemo(() => knownTimeZones(), []);
  const schedule = useScheduleText();
  const set = <K extends keyof CronForm>(field: K, value: CronForm[K]) => onChange({ ...form, [field]: value });
  const id = (field: string) => `${ids}-${field}`;
  const bad = (field: CronField) => wrong.includes(field);
  const error = (field: CronField) => (bad(field) ? t(`wrongField.${field}`) : null);
  const agents = form.agent && !choices.agents.includes(form.agent) ? [form.agent, ...choices.agents] : choices.agents;
  const plans = form.plan && !choices.plans.some((plan) => plan.id === form.plan) ? [{ id: form.plan, title: null }, ...choices.plans] : choices.plans;
  const chats = form.chat && !choices.chats.some((chat) => chat.id === form.chat) ? [{ id: form.chat, title: t("chatNumber", { id: form.chat }) }, ...choices.chats] : choices.chats;
  const cronWords = form.cron.trim().split(/\s+/).length === 5 ? schedule(form.cron) : null;
  return (
    <div className="flex flex-col gap-4">
      {isNew ? (
        <Field id={id("name")} label={t("name")} help={t("nameHelp")} error={error("name")}>
          <Input id={id("name")} value={form.name} onChange={(event) => set("name", event.target.value)} maxLength={63} autoComplete="off" spellCheck={false} className="font-mono" {...described(id("name"), bad("name"))} data-testid="cron-field-name" />
        </Field>
      ) : null}
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1.5 text-sm font-medium">{t("kind")}</legend>
        <RadioGroup value={form.kind} onValueChange={(value) => set("kind", value as CronKind)} className="grid gap-2 sm:grid-cols-3" data-testid="cron-field-kind">
          {CRON_KINDS.map((kind) => {
            const Icon = KIND_ICONS[kind];
            return (
              <Label key={kind} htmlFor={id(`kind-${kind}`)} className="flex cursor-pointer items-start gap-2 rounded-sm border p-3 font-normal has-data-[state=checked]:border-foreground has-data-[state=checked]:bg-accent">
                <RadioGroupItem value={kind} id={id(`kind-${kind}`)} className="mt-0.5" />
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5 font-medium">
                    <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
                    {kinds(kind)}
                  </span>
                  <span className="text-xs text-fg-subtle">{t(`kindHelp.${kind}`)}</span>
                </span>
              </Label>
            );
          })}
        </RadioGroup>
      </fieldset>
      {form.kind === "agent" ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id={id("agent")} label={t("agent")} error={error("agent")}>
              <NativeSelect id={id("agent")} value={form.agent} onChange={(event) => set("agent", event.target.value)} className="w-full" {...described(id("agent"), bad("agent"))} data-testid="cron-field-agent">
                <NativeSelectOption value="">{t("choose")}</NativeSelectOption>
                {agents.map((handle) => (
                  <NativeSelectOption key={handle} value={handle}>
                    @{handle}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </Field>
            <Field id={id("chat")} label={t("chat")} help={t("chatHelp")}>
              <NativeSelect id={id("chat")} value={form.chat} onChange={(event) => set("chat", event.target.value)} className="w-full" aria-describedby={`${id("chat")}-help`} data-testid="cron-field-chat">
                <NativeSelectOption value="">{t("directChat")}</NativeSelectOption>
                {chats.map((chat) => (
                  <NativeSelectOption key={chat.id} value={chat.id}>
                    {chat.title}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field id={id("prompt")} label={t("prompt")} help={t("promptHelp")} error={error("prompt")}>
            <Textarea id={id("prompt")} value={form.prompt} onChange={(event) => set("prompt", event.target.value)} rows={4} maxLength={MAX_PROMPT} {...described(id("prompt"), bad("prompt"))} data-testid="cron-field-prompt" />
          </Field>
        </>
      ) : form.kind === "plan" ? (
        <Field id={id("plan")} label={t("plan")} help={t("planHelp")} error={error("plan")}>
          <NativeSelect id={id("plan")} value={form.plan} onChange={(event) => set("plan", event.target.value)} className="w-full" {...described(id("plan"), bad("plan"))} data-testid="cron-field-plan">
            <NativeSelectOption value="">{t("choose")}</NativeSelectOption>
            {plans.map((plan) => (
              <NativeSelectOption key={plan.id} value={plan.id}>
                {plan.title ? `${plan.id}: ${plan.title}` : plan.id}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
      ) : (
        <>
          <Field id={id("command")} label={t("command")} help={t("commandHelp")} error={error("command")}>
            <Textarea id={id("command")} value={form.command} onChange={(event) => set("command", event.target.value)} rows={3} maxLength={MAX_COMMAND} spellCheck={false} className="font-mono text-[13px]" {...described(id("command"), bad("command"))} data-testid="cron-field-command" />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id={id("repo")} label={t("repo")} help={t("repoHelp")}>
              <NativeSelect id={id("repo")} value={form.repo} onChange={(event) => set("repo", event.target.value)} className="w-full" aria-describedby={`${id("repo")}-help`} data-testid="cron-field-repo">
                <NativeSelectOption value="">{t("noRepo")}</NativeSelectOption>
                {[...new Set([...(form.repo ? [form.repo] : []), ...choices.repos])].map((repo) => (
                  <NativeSelectOption key={repo} value={repo}>
                    {repo}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </Field>
            <Field id={id("timeout")} label={t("timeout")} help={t("timeoutHelp", { max: MAX_TIMEOUT })} error={error("timeout")}>
              <Input id={id("timeout")} type="number" inputMode="numeric" min={1} max={MAX_TIMEOUT} value={form.timeout} onChange={(event) => set("timeout", event.target.value)} className="tabular-nums" {...described(id("timeout"), bad("timeout"))} data-testid="cron-field-timeout" />
            </Field>
          </div>
        </>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={id("cron")} label={t("cron")} help={cronWords ?? t("cronHelp")} error={error("cron")}>
          <Input id={id("cron")} value={form.cron} onChange={(event) => set("cron", event.target.value)} autoComplete="off" spellCheck={false} className="font-mono" {...described(id("cron"), bad("cron"))} data-testid="cron-field-cron" />
        </Field>
        <Field id={id("timezone")} label={t("timezone")} help={t("timezoneHelp")} error={error("timezone")}>
          <Input id={id("timezone")} value={form.timezone} onChange={(event) => set("timezone", event.target.value)} list={`${id("timezone")}-zones`} autoComplete="off" spellCheck={false} {...described(id("timezone"), bad("timezone"))} data-testid="cron-field-timezone" />
          <datalist id={`${id("timezone")}-zones`}>
            {zones.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
        </Field>
      </div>
      <p className="text-xs text-fg-subtle">{t(form.kind === "command" ? "gapCommand" : "gapAgent")}</p>
    </div>
  );
}
