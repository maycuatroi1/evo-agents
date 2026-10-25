"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { TableSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";

import type { Actions } from "./account-columns";
import { AccountGroup } from "./account-group";
import { policyRows, serviceEntries } from "./model";
import { PolicySection } from "./policy-section";
import { type Account, policiesQuery } from "./queries";
import { ServiceTable } from "./service-table";

/** Service credentials: tokens and keys a run uses for something else than a model, with no cost. */
export function ServicesTab({ accounts, shared, now, ...actions }: { accounts: Account[]; shared: Set<string>; now: number } & Actions) {
  const members = accounts.filter((account) => account.category === "service");
  return (
    <AccountGroup id="service" count={members.length}>
      <ServiceTable entries={serviceEntries(members)} shared={shared} now={now} {...actions} />
    </AccountGroup>
  );
}

/** The policies of rotation, from the projects the accounts are bound to and the policies the visitor set. */
export function Policies({ accounts }: { accounts: Account[] }) {
  const t = useTranslations("accounts.policies");
  const policies = useQuery(policiesQuery(browserApi));
  if (policies.isPending) return <TableSkeleton rows={2} />;
  if (policies.isError) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="account-policies-error">
        {t("loadFailed")}
      </p>
    );
  }
  return <PolicySection rows={policyRows(accounts, policies.data)} />;
}
