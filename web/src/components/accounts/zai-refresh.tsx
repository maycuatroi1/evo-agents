"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { signInAgainOn401, useFailure } from "./hooks";
import { type QuotaTarget, refreshQuota } from "./provider-queries";
import { accountKeys } from "./queries";

/**
 * Refresh of a Z.ai Coding Plan account's usage: the hub reads its quota from Z.ai now instead of at its next sync, and
 * the bars show it. `agent` names the agent whose account it is, on a project's pools.
 */
export function ZaiRefresh({ account, agent }: { account: string; agent?: QuotaTarget["agent"] }) {
  const t = useTranslations("accounts.zai");
  const failureOf = useFailure();
  const queryClient = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => refreshQuota(browserApi(), { account, agent }),
    onError: signInAgainOn401,
    onSettled: () => queryClient.invalidateQueries({ queryKey: accountKeys.all }),
  });
  const run = async () => {
    try {
      const answer = await refresh.mutateAsync();
      if (answer.ok) notify({ tone: "success", text: t("refreshed", { name: account }) });
      else notify({ tone: "info", text: t("noQuota", { name: account }), description: answer.error ?? t("noQuotaText") });
    } catch (error) {
      notifyFailure(t("failed", { name: account }), failureOf(error));
    }
  };
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-7 px-2 text-xs"
      busy={refresh.isPending}
      aria-label={t("refreshLabel", { name: account })}
      onClick={() => void run()}
      data-testid="zai-refresh"
    >
      <RefreshCw aria-hidden="true" />
      {t("refresh")}
    </Button>
  );
}
