"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { budgetFormOf, MAX_WEEKLY_BUDGET_USD, readBudget } from "./budget-model";
import { FormDialog } from "./form-parts";
import { type Failure, useChangeAccount, useFailure } from "./hooks";
import type { Account } from "./queries";

/**
 * The weekly budget of a model account, in dollars: the hub draws the cost of its runs this week over it as the
 * account's weekly window, which quota-pacing and the Token strategy read. Empty drops it.
 */
export function BudgetDialog({ account, open, onOpenChange }: { account: Account; open: boolean; onOpenChange: (open: boolean) => void }) {
  return <BudgetFormDialog key={`${account.name}-${String(open)}`} account={account} open={open} onOpenChange={onOpenChange} />;
}

function BudgetFormDialog({ account, open, onOpenChange }: { account: Account; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("accounts.budget");
  const id = useId();
  const write = useChangeAccount();
  const failureOf = useFailure();
  const [text, setText] = useState(() => budgetFormOf(account));
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const read = readBudget(text);
  const problem = sent && read.problem ? t(`problems.${read.problem}`, { max: MAX_WEEKLY_BUDGET_USD }) : null;

  const save = async () => {
    setSent(true);
    setFailure(null);
    if (read.problem) return;
    try {
      await write.mutateAsync({ name: account.name, body: { weekly_budget_usd: read.value } });
    } catch (error) {
      setFailure(failureOf(error));
      return;
    }
    onOpenChange(false);
    notify({ tone: "success", text: t(read.value === null ? "dropped" : "saved", { name: account.name }), description: t("savedText") });
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
      testId="budget-dialog"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>{t("label")}</Label>
        <Input
          id={id}
          type="text"
          inputMode="decimal"
          value={text}
          onChange={(event) => setText(event.target.value)}
          autoComplete="off"
          placeholder={t("placeholder")}
          className="h-9 w-full tabular-nums sm:w-40"
          aria-invalid={problem ? true : undefined}
          aria-describedby={problem ? `${id}-hint ${id}-error` : `${id}-hint`}
          data-testid="budget-input"
        />
        <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
          {t("hint")}
        </p>
        {problem ? (
          <p id={`${id}-error`} className="text-xs font-medium text-danger" data-testid="budget-input-error">
            {problem}
          </p>
        ) : null}
      </div>
    </FormDialog>
  );
}
