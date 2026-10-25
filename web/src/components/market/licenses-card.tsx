"use client";

import { KeyRound, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";

import { useMoney } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";

import { useLicenseCheck, useMarketFailure } from "./data";
import { LicenseBadge } from "./parts";
import { licensesQuery, type Purchase } from "./queries";

function PurchaseRow({ purchase }: { purchase: Purchase }) {
  const t = useTranslations("market.licenses");
  const money = useMoney();
  const state = purchase.license_state ?? (purchase.state === "open" ? "pending" : purchase.state);
  return (
    <li className="flex min-w-0 flex-col gap-1 py-2.5 first:pt-0 last:pb-0" data-purchase={purchase.number}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium [overflow-wrap:anywhere]">{purchase.plugin}</span>
        <LicenseBadge state={state} />
      </div>
      <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {t("line", { number: purchase.number, seller: purchase.seller, months: purchase.months, total: money(purchase.usd_total) })}
      </span>
      {purchase.valid_until ? <span className="text-xs text-fg-subtle">{t("until", { day: purchase.valid_until })}</span> : null}
      {purchase.last_error ? <span className="text-xs text-danger [overflow-wrap:anywhere]">{purchase.last_error}</span> : null}
      {purchase.checked_at ? (
        <span className="text-xs text-fg-subtle">
          {t("checked")} <When value={purchase.checked_at} />
        </span>
      ) : null}
    </li>
  );
}

/**
 * The hub's orders of paid plugins, for hub admins (docs/market.md, Selling plugins): each with its seller, months,
 * total and where its licence stands, the hub's id that licences name, and Check now, which asks the sellers about the
 * open orders as the hourly job does, installs what a licence covers and turns off what lapsed.
 */
export function LicensesCard() {
  const t = useTranslations("market.licenses");
  const state = useHubQuery(licensesQuery(browserApi));
  const check = useLicenseCheck();
  const failure = useMarketFailure();
  const found = state.status === "success" ? state.data : null;

  const onCheck = async () => {
    try {
      const done = await check.mutateAsync();
      notify({
        tone: done.errors.length ? "info" : "success",
        text: t("checkedNow", { count: done.checked }),
        description: [...done.installed.map((name) => t("installed", { name })), ...done.lapsed.map((name) => t("lapsed", { name })), ...done.errors].join("\n") || null,
      });
    } catch (error) {
      notifyFailure(t("checkFailed"), failure(error));
    }
  };

  return (
    <Card className="min-w-0 self-start" data-testid="market-licenses">
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <KeyRound className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-3">
        {found ? (
          <p className="text-xs text-muted-foreground">
            {t("hub")} <Identifier value={found.hub} />
          </p>
        ) : null}
        {found && found.purchases.length > 0 ? (
          <ul className="flex flex-col divide-y">
            {found.purchases.map((purchase) => (
              <PurchaseRow key={purchase.id} purchase={purchase} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        )}
        <Button size="sm" variant="outline" className="self-start" busy={check.isPending} onClick={() => void onCheck()} data-testid="market-licenses-check">
          <RefreshCw aria-hidden="true" />
          {check.isPending ? t("checking") : t("check")}
        </Button>
      </CardContent>
    </Card>
  );
}
