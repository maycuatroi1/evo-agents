"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { NativeSelect, NativeSelectOptGroup, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";

import {
  accountsQuery,
  BY_RULES,
  KEEP,
  pickAccount,
  projectAccounts,
  projectSharedAccounts,
  sharedAccountsQuery,
} from "./account-model";
import { FieldLabel } from "./field-label";
import type { RequestedRuntime } from "./queries";

/**
 * The account of a dispatch form that fills it by itself (Dispatch, Run plan): the one the visitor picked, else the
 * account found for the runtime and the worker (`pickAccount`), else the rules. The accounts are read once, shared
 * with the field.
 */
export function useAccountChoice(project: string, runtime: RequestedRuntime, worker: string | null): [string, (value: string) => void] {
  const accounts = useQuery(accountsQuery(browserApi));
  const [picked, pick] = useState<string | null>(null);
  return [picked ?? pickAccount(accounts.data ?? [], project, runtime, worker), pick];
}

/**
 * The account a run takes, in the dispatch forms (Dispatch, Run plan, New plan, Revise with agent) and in Rerun: auto,
 * by the rules of the visitor's policy for the project, or one of the visitor's accounts bound to the project, picked
 * by hand, which the run keeps with no failover. A paused or expired account is listed but cannot be picked; one bound
 * to some workers only says which, since the hub takes it only for a run pinned to one of them. The accounts other
 * members share with the visitor for the project follow in a group of their own, by `<owner>/<name>`; one its owner
 * paused or that ended is listed but cannot be picked. With `rerunOf`, the
 * first choice keeps the account picked by hand for that run. When the accounts cannot be read, only auto is offered,
 * so a dispatch never waits on them. What a choice means shows from the info icon.
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
  const shared = useQuery(sharedAccountsQuery(browserApi));
  const bound = projectAccounts(accounts.data ?? [], project);
  const lent = projectSharedAccounts(shared.data ?? [], project);
  const info =
    rerunOf !== undefined
      ? t("rerunHint", { id: rerunOf })
      : bound.length === 0 && lent.length === 0 && accounts.isSuccess
        ? t("none", { project })
        : t("hint");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="dispatch-account">
      <FieldLabel htmlFor={`${ids}-account`} label={t("label")} info={info} testId="dispatch-account-info" />
      <NativeSelect
        id={`${ids}-account`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="w-full md:[&_select]:h-9"
        data-testid="dispatch-account-select"
      >
        {rerunOf !== undefined ? <NativeSelectOption value={KEEP}>{t("keep", { id: rerunOf })}</NativeSelectOption> : null}
        <NativeSelectOption value={BY_RULES}>{t("byRules")}</NativeSelectOption>
        {bound.map((account) => (
          <NativeSelectOption key={account.name} value={account.name} disabled={account.state === "paused" || account.state === "expired"}>
            {account.state === "paused" || account.state === "expired" || account.state === "cooling"
              ? t(account.state, { name: account.name, variable: account.env_var })
              : account.workers.length > 0
                ? t("onWorkers", { name: account.name, variable: account.env_var, workers: account.workers.join(", ") })
                : t("option", { name: account.name, variable: account.env_var })}
          </NativeSelectOption>
        ))}
        {lent.length > 0 ? (
          <NativeSelectOptGroup label={t("sharedGroup")} data-testid="dispatch-account-shared">
            {lent.map((account) => {
              const values = { name: account.name, owner: account.owner, variable: account.env_var };
              return (
                <NativeSelectOption
                  key={account.pick}
                  value={account.pick}
                  disabled={account.state === "paused" || account.state === "expired"}
                >
                  {account.state === "paused"
                    ? t("shared_paused", values)
                    : account.state === "expired"
                      ? t("shared_expired", values)
                      : account.state === "cooling"
                        ? t("shared_cooling", values)
                        : t("shared", values)}
                </NativeSelectOption>
              );
            })}
          </NativeSelectOptGroup>
        ) : null}
      </NativeSelect>
    </div>
  );
}
