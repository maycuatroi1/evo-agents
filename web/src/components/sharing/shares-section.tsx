"use client";

import { useMutation } from "@tanstack/react-query";
import { Building2, Share2, User, Users } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import { type CredentialShare, endsSoon, type ShareTarget } from "./model";
import { revokeShare } from "./queries";
import { LazyShareDialog, preloadShare } from "./share-lazy";

const GRANTEE_ICONS = { user: User, unit: Building2, all: Users } as const;

/** Whom a share is for: a member's login, a unit's slug, or every member. */
function Grantee({ share }: { share: CredentialShare }) {
  const t = useTranslations("sharing.list");
  const Icon = GRANTEE_ICONS[share.grantee_kind];
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5" data-testid="share-grantee" data-kind={share.grantee_kind}>
      <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {share.grantee_kind === "all" ? t("all") : t(share.grantee_kind, { name: share.grantee ?? "" })}
      </span>
    </span>
  );
}

/** The limits a share sets, in one line; none when it sets none. */
function useLimits() {
  const t = useTranslations("sharing.list");
  const format = useFormatter();
  return (share: CredentialShare): string => {
    const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
    const parts = [
      share.level ? t("level", { level: share.level }) : null,
      share.budget_usd !== null ? t("budget", { usd: usd(share.budget_usd) }) : null,
      share.usage_ceiling_pct !== null ? t("ceiling", { pct: share.usage_ceiling_pct }) : null,
      share.max_running !== null ? t("running", { count: share.max_running }) : null,
    ].filter((part): part is string => part !== null);
    return parts.length > 0 ? parts.join(", ") : t("noLimit");
  };
}

function ShareRow({
  share,
  now,
  extra,
  onRevoke,
}: {
  share: CredentialShare;
  now: number | null;
  extra?: ReactNode;
  onRevoke: (share: CredentialShare) => void;
}) {
  const t = useTranslations("sharing.list");
  const limits = useLimits();
  const soon = now !== null && endsSoon(share, now);
  return (
    <li className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:justify-between" data-testid="share-row" data-share-id={share.id}>
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium">
          <span className="font-mono text-[13px] [overflow-wrap:anywhere]" data-testid="share-secret-name">
            {share.secret}
          </span>
          <span aria-hidden="true" className="text-muted-foreground">
            {"->"}
          </span>
          <span className="sr-only">{t("to")}</span>
          <Grantee share={share} />
          {soon ? (
            <Badge variant="warning" data-testid="share-ends-soon">
              {t("endsSoon")}
            </Badge>
          ) : null}
          {extra}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <ul className="flex flex-wrap gap-1" aria-label={t("projects")}>
            {share.projects.map((project) => (
              <li key={project} className="max-w-full min-w-0">
                <Identifier value={project} />
              </li>
            ))}
          </ul>
          <span data-testid="share-limits">{limits(share)}</span>
          {share.expires_at ? <span>{t.rich("ends", { day: () => <When value={share.expires_at} /> })}</span> : null}
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        onClick={() => onRevoke(share)}
        aria-label={t("revokeLabel", { name: share.secret, id: share.id })}
        data-testid="share-revoke"
      >
        {t("revoke")}
      </Button>
    </li>
  );
}

/**
 * The visitor's live shares of some secrets or accounts, newest first, each with whom it is for, its projects, its
 * limits and when it ends, and Revoke behind a confirmation; Share opens the dialog. Only what is unusual about a share
 * shows as a pill: it ends within a week, or what `extra` says of it (an account above the share's ceiling).
 */
export function SharesSection({
  targets,
  shares,
  onChanged,
  extra,
  now,
  testId,
}: {
  targets: ShareTarget[];
  shares: CredentialShare[];
  onChanged: () => Promise<unknown>;
  extra?: (share: CredentialShare) => ReactNode;
  now: number | null;
  testId: string;
}) {
  const t = useTranslations("sharing");
  const failureOf = useWriteFailure();
  const [dialog, setDialog] = useState<{ open: boolean } | null>(null);
  const [revoking, setRevoking] = useState<{ open: boolean; share: CredentialShare | null }>({ open: false, share: null });
  const [error, setError] = useState<WriteFailure | null>(null);
  const revoke = useMutation({
    mutationFn: (id: number) => revokeShare(browserApi(), id),
    onError: (caught) => {
      if (isApiError(caught) && caught.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: onChanged,
  });
  const confirm = async () => {
    const share = revoking.share;
    if (!share) return;
    setError(null);
    try {
      await revoke.mutateAsync(share.id);
    } catch (caught) {
      setError(failureOf(caught, { 409: t("revoke.gone"), 404: t("revoke.gone") }));
      return;
    }
    setRevoking((current) => ({ ...current, open: false }));
    notify({ tone: "success", text: t("revoke.done", { name: share.secret }), description: t("revoke.doneText") });
  };
  const titleId = `${testId}-title`;
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3" data-testid={testId}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id={titleId} className="text-[15px] leading-[22px] font-semibold">
            {t("title")}
          </h2>
          <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={() => setDialog({ open: true })}
          onPointerEnter={preloadShare}
          onFocus={preloadShare}
          disabled={targets.length === 0}
          aria-describedby={targets.length === 0 ? `${testId}-none` : undefined}
          data-testid="share-open"
        >
          <Share2 aria-hidden="true" />
          {t("share")}
        </Button>
      </div>
      {targets.length === 0 ? (
        <p id={`${testId}-none`} className="text-xs text-pretty text-muted-foreground" data-testid="share-none">
          {t("nothingToShare")}
        </p>
      ) : null}
      {shares.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="shares-empty">
          {t("empty")}
        </p>
      ) : (
        <ul className="divide-y rounded-md border bg-card shadow-raised" aria-label={t("title")} data-testid="shares-list">
          {shares.map((share) => (
            <ShareRow
              key={share.id}
              share={share}
              now={now}
              extra={extra?.(share)}
              onRevoke={(picked) => {
                setError(null);
                setRevoking({ open: true, share: picked });
              }}
            />
          ))}
        </ul>
      )}
      {dialog ? (
        <LazyShareDialog
          open={dialog.open}
          onOpenChange={(open) => setDialog({ open })}
          targets={targets}
          onChanged={onChanged}
        />
      ) : null}
      <ConfirmAction
        open={revoking.open}
        onOpenChange={(open) => setRevoking((current) => ({ ...current, open }))}
        title={t("revoke.title", { name: revoking.share?.secret ?? "" })}
        description={<p>{t("revoke.description")}</p>}
        confirmLabel={t("revoke.confirm")}
        pendingLabel={t("revoke.pending")}
        cancelLabel={t("revoke.cancel")}
        pending={revoke.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="share-revoke-dialog"
      />
    </section>
  );
}
