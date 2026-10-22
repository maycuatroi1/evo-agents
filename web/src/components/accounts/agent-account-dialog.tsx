"use client";

import { useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { MAX_ENV_VAR_CHARS } from "@/lib/secret-rules";

import { EndField, TextField, ValueField, WorkersField } from "./agent-account-fields";
import { useAgentAccountFailure, useChangeAgentAccount, usePutAgentAccount } from "./agent-hooks";
import {
  addProblems,
  type AgentAccountForm,
  agentFormOf,
  type AgentField,
  type AgentProblem,
  type AgentProblems,
  changeBody,
  editProblems,
  emptyAgentForm,
  putBody,
  renewBody,
  renewProblems,
} from "./agent-model";
import type { AgentAccount } from "./agent-queries";
import { FormDialog, NumberField } from "./form-parts";
import type { Failure } from "./hooks";
import { MAX_SECRET_BYTES, MAX_SECRET_NAME_CHARS } from "./login-queries";
import { MAX_PRIORITY, MAX_WEIGHT } from "./queries";

/**
 * Add, Edit and Renew of an agent's account, for an admin of its project. Add takes a name, a variable, the value, the
 * workers and an end; Edit its weight, priority, workers and end; Renew a new value and, if given, a new end. The value
 * sits in a password field the page never fills: the dialog reads it when it is sent, and closing it drops it.
 */
export type AgentDialogKind = "add" | "edit" | "renew";

type Props = {
  project: string;
  handle: string;
  kind: AgentDialogKind;
  account: AgentAccount | null;
  taken: readonly string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

export function AgentAccountDialog(props: Props) {
  // Keyed by the opening, so each one starts empty, or from what the hub holds.
  return <AgentAccountForm key={`${props.kind}-${props.account?.name ?? ""}-${String(props.open)}`} {...props} />;
}

function useProblemText() {
  const t = useTranslations("accounts.agent.form.problems");
  return (problem: AgentProblem | undefined, max = 0) =>
    problem ? t(problem, { max, min: 0, bytes: MAX_SECRET_BYTES, chars: MAX_SECRET_NAME_CHARS }) : null;
}

function AgentAccountForm({ project, handle, kind, account, taken, open, onOpenChange }: Props) {
  const t = useTranslations("accounts.agent.form");
  const ids = useId();
  const say = useProblemText();
  const put = usePutAgentAccount(project, handle);
  const change = useChangeAgentAccount(project, handle);
  const failureOf = useAgentAccountFailure();
  const valueRef = useRef<HTMLInputElement>(null);
  const [form, setForm] = useState<AgentAccountForm>(() => (account && kind === "edit" ? agentFormOf(account) : emptyAgentForm()));
  const [problems, setProblems] = useState<AgentProblems>({});
  const [failure, setFailure] = useState<Failure | null>(null);
  const set = (patch: Partial<AgentAccountForm>, field: AgentField) => {
    setForm((current) => ({ ...current, ...patch }));
    setProblems(({ [field]: _gone, ...rest }) => rest);
  };
  const name = account?.name ?? form.name.trim();

  const save = async () => {
    setFailure(null);
    const value = valueRef.current?.value ?? "";
    const now = Date.now();
    const found =
      kind === "add" ? addProblems(form, value, taken, now) : kind === "renew" ? renewProblems(value, form.expires, now) : account ? editProblems(form, account, now) : {};
    setProblems(found);
    if (Object.keys(found).length > 0) return;
    try {
      if (kind === "add") {
        await put.mutateAsync({ name, body: putBody({ ...form, name }, value) });
      } else if (kind === "renew") {
        await change.mutateAsync({ name, body: renewBody(value, form.expires) });
      } else if (account) {
        const body = changeBody(form, account);
        if (Object.keys(body).length > 0) await change.mutateAsync({ name, body });
      }
    } catch (error) {
      setFailure(failureOf(error));
      return;
    } finally {
      // The value leaves the page once the hub answered, whatever it said.
      if (valueRef.current) valueRef.current.value = "";
    }
    onOpenChange(false);
    notify({ tone: "success", text: t(`${kind}.done`, { name, handle }), description: t(`${kind}.doneText`, { handle }) });
  };

  const field = (key: AgentField, label: string, hint: string, testId: string) => ({
    id: `${ids}-${key}`,
    label,
    hint,
    problem: say(problems[key], key === "weight" ? MAX_WEIGHT : key === "priority" ? MAX_PRIORITY : MAX_ENV_VAR_CHARS),
    testId,
  });

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t(`${kind}.title`, { name, handle })}
      description={t(`${kind}.description`, { handle })}
      pending={put.isPending || change.isPending}
      failure={failure}
      labels={{ save: t(`${kind}.save`), saving: t("saving"), cancel: t("cancel") }}
      onSubmit={() => void save()}
      testId="agent-account-dialog"
    >
      {kind === "add" ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            {...field("name", t("name"), t("nameHint"), "agent-account-name")}
            value={form.name}
            onChange={(next) => set({ name: next }, "name")}
            maxLength={MAX_SECRET_NAME_CHARS}
          />
          <TextField
            {...field("envVar", t("envVar"), t("envVarHint"), "agent-account-env")}
            value={form.envVar}
            onChange={(next) => set({ envVar: next.toUpperCase() }, "envVar")}
            maxLength={MAX_ENV_VAR_CHARS}
          />
        </div>
      ) : null}
      {kind !== "edit" ? <ValueField {...field("value", t("value"), t("valueHint"), "agent-account-value")} inputRef={valueRef} /> : null}
      {kind !== "renew" ? (
        <WorkersField
          id={`${ids}-workers`}
          project={project}
          picked={form.workers}
          current={account?.workers ?? []}
          problem={say(problems.workers)}
          onChange={(workers) => set({ workers }, "workers")}
        />
      ) : null}
      {kind === "edit" ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <NumberField
            {...field("weight", t("weight"), t("weightHint", { max: MAX_WEIGHT }), "agent-account-weight")}
            value={form.weight}
            onChange={(weight) => set({ weight }, "weight")}
          />
          <NumberField
            {...field("priority", t("priority"), t("priorityHint", { max: MAX_PRIORITY }), "agent-account-priority")}
            value={form.priority}
            onChange={(priority) => set({ priority }, "priority")}
          />
        </div>
      ) : null}
      <EndField
        {...field("expires", t("expires"), t(kind === "renew" ? "expiresRenewHint" : "expiresHint"), "agent-account-expires")}
        value={form.expires}
        onChange={(expires) => set({ expires }, "expires")}
      />
    </FormDialog>
  );
}
