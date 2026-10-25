"use client";

import { useQuery } from "@tanstack/react-query";
import { KeyRound, Trash2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify } from "@/components/feedback/toast";
import { StatusPill } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNow } from "@/hooks/use-now";
import { browserApi } from "@/lib/api/browser";

import { Ago } from "./ago";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { EXPIRING_LOOK } from "./pool-badges";
import { expiringSoon } from "./pool-model";
import { type JoinToken, poolTokensQuery, revokeJoinToken } from "./queries";
import { SectionCard } from "./section-card";

/**
 * A pool's join tokens, newest first, without the tokens themselves: when each ends, how many machines joined with it
 * and who made it. A token that still works shows no state; one about to end says so, and one that ended says how.
 * A token that still works may be revoked, so no more machine joins with it; the nodes that joined stay.
 */
export function PoolTokens({ pool, onCreate }: { pool: string; onCreate: () => void }) {
  const t = useTranslations("workers.pools.tokens");
  const tokens = useQuery(poolTokensQuery(browserApi, pool));
  const [revoking, setRevoking] = useState<JoinToken | null>(null);

  return (
    <SectionCard title={t("title")} count={tokens.data?.length} description={t("description")} testId="pool-tokens">
      {tokens.isPending ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : tokens.isError ? (
        <p className="text-sm text-danger" role="alert">
          {t("failed")}
        </p>
      ) : tokens.data.length === 0 ? (
        <div className="flex flex-col items-start gap-3 rounded-md border border-dashed px-4 py-4 text-sm text-muted-foreground">
          <p>{t("empty")}</p>
          <Button variant="outline" size="sm" onClick={onCreate} data-testid="pool-tokens-create">
            <KeyRound aria-hidden="true" />
            {t("create")}
          </Button>
        </div>
      ) : (
        <ul className="flex flex-col divide-y">
          {tokens.data.map((token) => (
            <TokenRow key={token.id} token={token} onRevoke={() => setRevoking(token)} />
          ))}
        </ul>
      )}
      <RevokeToken pool={pool} token={revoking} onClose={() => setRevoking(null)} />
    </SectionCard>
  );
}

function TokenRow({ token, onRevoke }: { token: JoinToken; onRevoke: () => void }) {
  const t = useTranslations("workers.pools.tokens");
  const format = useFormatter();
  const now = useNow(token.status === "live");
  const soon = expiringSoon(token, now);
  const expires = format.dateTime(new Date(token.expires_at), { dateStyle: "medium", timeStyle: "short" });
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 py-3 first:pt-0 last:pb-0" data-testid="pool-token" data-token-id={token.id}>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-mono">{t("name", { id: token.id })}</span>
          {soon ? <StatusPill look={EXPIRING_LOOK} label={t("expiringSoon")} data-testid="pool-token-expiring" /> : null}
          {token.status !== "live" ? (
            <Badge variant="outline" className="text-[11px]" data-testid="pool-token-status">
              {t(`status.${token.status}`)}
            </Badge>
          ) : null}
        </span>
        <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
          {t("line", { uses: token.uses, max: token.max_uses, by: token.created_by, expires })}
          {token.last_used_at ? (
            <>
              {", "}
              {t("lastUsed")} <Ago value={token.last_used_at} never="" />
            </>
          ) : null}
        </span>
      </div>
      {token.status === "live" ? (
        <Button variant="outline" size="sm" onClick={onRevoke} data-testid="pool-token-revoke">
          <Trash2 aria-hidden="true" />
          {t("revoke")}
        </Button>
      ) : null}
    </li>
  );
}

function RevokeToken({ pool, token, onClose }: { pool: string; token: JoinToken | null; onClose: () => void }) {
  const t = useTranslations("workers.pools.tokens");
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, id: number) => revokeJoinToken(api, pool, id));
  const close = () => {
    write.reset();
    onClose();
  };
  return (
    <ConfirmAction
      open={token !== null}
      onOpenChange={(open) => (open ? undefined : close())}
      title={t("revokeTitle", { id: token?.id ?? 0 })}
      description={<p>{t("revokeText")}</p>}
      confirmLabel={t("revoke")}
      pendingLabel={t("revoking")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={write.isError ? failure(write.error, { 409: t("revokedAlready") }) : null}
      onConfirm={() => {
        if (!token) return;
        write.mutate(token.id, {
          onSuccess: () => {
            close();
            notify({ tone: "success", text: t("revoked", { id: token.id }), description: t("revokedText") });
          },
        });
      }}
      testId="pool-token-revoke-dialog"
    />
  );
}
