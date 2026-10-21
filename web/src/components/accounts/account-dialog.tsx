"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { notify } from "@/components/feedback/toast";

import { FormDialog, NumberField } from "./form-parts";
import { type Failure, useChangeAccount, useFailure } from "./hooks";
import { accountBody, type AccountForm, accountFormOf, accountProblems, type Problem } from "./model";
import { type Account, MAX_PRIORITY, MAX_WEIGHT } from "./queries";

/** An account's weight and priority, the two numbers by which a pool of the same variable picks it. */
export function AccountDialog({ account, open, onOpenChange }: { account: Account | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  if (!account) return null;
  // Keyed by the opening, so each one starts from what the hub holds.
  return <AccountFormDialog key={`${account.name}-${String(open)}`} account={account} open={open} onOpenChange={onOpenChange} />;
}

function AccountFormDialog({ account, open, onOpenChange }: { account: Account; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("accounts.form");
  const ids = useId();
  const write = useChangeAccount();
  const failureOf = useFailure();
  const [form, setForm] = useState<AccountForm>(() => accountFormOf(account));
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const problems = sent ? accountProblems(form) : {};
  const say = (problem: Problem | undefined, max: number) =>
    problem === "whole" ? t("problems.whole") : problem === "range" ? t("problems.range", { min: 0, max }) : null;

  const save = async () => {
    setSent(true);
    setFailure(null);
    if (Object.keys(accountProblems(form)).length > 0) return;
    try {
      await write.mutateAsync({ name: account.name, body: accountBody(form) });
    } catch (error) {
      setFailure(failureOf(error));
      return;
    }
    onOpenChange(false);
    notify({ tone: "success", text: t("saved", { name: account.name }), description: t("savedText") });
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("title", { name: account.name })}
      description={t("description")}
      pending={write.isPending}
      failure={failure}
      labels={{ save: t("save"), saving: t("saving"), cancel: t("cancel") }}
      onSubmit={() => void save()}
      testId="account-dialog"
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <NumberField
          id={`${ids}-weight`}
          label={t("weight")}
          hint={t("weightHint", { max: MAX_WEIGHT })}
          problem={say(problems.weight, MAX_WEIGHT)}
          value={form.weight}
          onChange={(weight) => setForm((current) => ({ ...current, weight }))}
          testId="account-weight"
        />
        <NumberField
          id={`${ids}-priority`}
          label={t("priority")}
          hint={t("priorityHint", { max: MAX_PRIORITY })}
          problem={say(problems.priority, MAX_PRIORITY)}
          value={form.priority}
          onChange={(priority) => setForm((current) => ({ ...current, priority }))}
          testId="account-priority"
        />
      </div>
    </FormDialog>
  );
}
