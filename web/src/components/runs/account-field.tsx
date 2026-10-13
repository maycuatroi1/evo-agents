"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";

import { accountsQuery, BY_RULES, KEEP, projectAccounts } from "./account-model";

/**
 * The account a run takes, in the dispatch forms (Dispatch, Run plan, New plan, Revise with agent) and in Rerun: auto,
 * by the rules of the visitor's policy for the project (the default), or one of the visitor's accounts bound to the
 * project, picked by hand, which the run keeps with no failover. A paused or expired account is listed but cannot be
 * picked; one bound to some workers only says which, since the hub takes it only for a run pinned to one of them. With
 * `rerunOf`, the first choice keeps the account picked by hand for that run. When the accounts cannot be read, only
 * auto is offered, so a dispatch never waits on them.
 */
export function AccountField({
  project,
  value,
  onChange,
  disabled = false,
  rerunOf,
}: {
  project: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  rerunOf?: number;
}) {
  const t = useTranslations("runs.account");
  const ids = useId();
  const accounts = useQuery(accountsQuery(browserApi));
  const bound = projectAccounts(accounts.data ?? [], project);
  const hint = rerunOf !== undefined ? t("rerunHint", { id: rerunOf }) : bound.length === 0 && accounts.isSuccess ? t("none", { project }) : t("hint");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="dispatch-account">
      <label htmlFor={`${ids}-account`} className="text-sm font-medium">
        {t("label")}
      </label>
      <NativeSelect
        id={`${ids}-account`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-describedby={`${ids}-hint`}
        className="w-full sm:w-80 md:[&_select]:h-9"
        data-testid="dispatch-account-select"
      >
        {rerunOf !== undefined ? <NativeSelectOption value={KEEP}>{t("keep", { id: rerunOf })}</NativeSelectOption> : null}
        <NativeSelectOption value={BY_RULES}>{t("byRules")}</NativeSelectOption>
        {bound.map((account) => (
          <NativeSelectOption key={account.name} value={account.name} disabled={account.state === "paused" || account.state === "expired"}>
            {account.state === "paused" || account.state === "expired"
              ? t(account.state, { name: account.name, variable: account.env_var })
              : account.workers.length > 0
                ? t("onWorkers", { name: account.name, variable: account.env_var, workers: account.workers.join(", ") })
                : t("option", { name: account.name, variable: account.env_var })}
          </NativeSelectOption>
        ))}
      </NativeSelect>
      <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground">
        {hint}
      </p>
    </div>
  );
}
