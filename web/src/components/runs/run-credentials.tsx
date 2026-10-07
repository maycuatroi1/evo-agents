"use client";

import { useQuery } from "@tanstack/react-query";
import { Ban, CircleDot, KeyRound, TimerOff } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { GitHubMark } from "@/components/brand";
import { Tag } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { SECRETS_HREF } from "@/components/secrets/queries";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";

import { isActiveState, type Run, type RunLease, runCredentialsQuery } from "./queries";
import { type LeaseState, leaseState, leaseTargets } from "./run-model";

/**
 * The credentials a run got from the hub (docs/credentials.md, What a run got), a card of the run page's side column:
 * each lease's name, its provider as a tag, what it answered for, when it was issued, when it ends and when it was
 * given back, and its state as a pill. Never a value: the hub's answer carries none. Shown to the member who
 * dispatched the run alone, as the API answers only them.
 */

/** A lease's state in the kit's tones: out is held by the run's worker now (`brand`), the others are over. */
const STATES: Record<LeaseState, { icon: typeof CircleDot; variant: "info" | "secondary" | "outline" }> = {
  out: { icon: CircleDot, variant: "info" },
  expired: { icon: TimerOff, variant: "secondary" },
  revoked: { icon: Ban, variant: "outline" },
};

function When({ at }: { at: string }) {
  const format = useFormatter();
  const date = new Date(at);
  return (
    <time dateTime={at} title={format.dateTime(date, { dateStyle: "full", timeStyle: "medium" })} className="tabular-nums">
      {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

function LeaseItem({ lease, now }: { lease: RunLease; now: number | null }) {
  const t = useTranslations("runs.detail.credentials");
  const state = now === null ? (lease.revoked_at ? "revoked" : "out") : leaseState(lease, now);
  const { icon: StateIcon, variant } = STATES[state];
  const targets = leaseTargets(lease);
  return (
    <li className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0" data-testid="run-lease-item" data-lease-id={lease.id} data-state={state}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[13px] leading-5 font-medium text-foreground [overflow-wrap:anywhere]" data-testid="run-lease-name">
          {lease.name}
        </span>
        <Tag data-testid="run-lease-provider" data-provider={lease.provider}>
          {lease.provider === "github-app" ? <GitHubMark /> : <KeyRound aria-hidden="true" />}
          {t(`provider.${lease.provider === "github-app" ? "githubApp" : "secret"}`)}
        </Tag>
        <Badge variant={variant} className="ml-auto" data-testid="run-lease-state">
          <StateIcon aria-hidden="true" />
          {t(`state.${state}`)}
        </Badge>
      </div>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted-foreground">{lease.kind === "env" ? t("variable") : t("repos")}</dt>
        <dd className="min-w-0" data-testid="run-lease-target">
          <ul className="flex flex-col gap-0.5 font-mono [overflow-wrap:anywhere]">
            {targets.map((target) => (
              <li key={target}>{target}</li>
            ))}
          </ul>
        </dd>
        <dt className="text-muted-foreground">{t("issued")}</dt>
        <dd className="min-w-0 [overflow-wrap:anywhere]" data-testid="run-lease-issued">
          {t.rich("issuedTo", { worker: lease.worker, time: () => <When at={lease.issued_at} />, code: (chunks) => <span className="font-mono text-foreground">{chunks}</span> })}
        </dd>
        <dt className="text-muted-foreground">{t("expires")}</dt>
        <dd data-testid="run-lease-expires">{lease.expires_at ? <When at={lease.expires_at} /> : <span className="text-fg-subtle">{t("noEnd")}</span>}</dd>
        <dt className="text-muted-foreground">{t("revoked")}</dt>
        <dd data-testid="run-lease-revoked">{lease.revoked_at ? <When at={lease.revoked_at} /> : <span className="text-fg-subtle">{t("notRevoked")}</span>}</dd>
      </dl>
    </li>
  );
}

/** The run's leases, read every 5 seconds while it is active, for its owner. */
export function RunCredentials({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.credentials");
  const id = useId();
  const active = isActiveState(run.state);
  const query = useQuery(runCredentialsQuery(browserApi, run.project, run.id, active));
  const leases = query.data ?? [];
  const now = useNow(leases.some((lease) => lease.revoked_at === null && lease.expires_at !== null));

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid="run-credentials">
      <div className="flex flex-col gap-0.5 border-b px-4 py-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-xs text-pretty text-fg-subtle">{t("description")}</p>
      </div>
      <div className="min-w-0 px-4 py-3 text-sm">
        {query.isPending ? (
          <div className="flex flex-col gap-2" role="status" aria-label={t("loading")}>
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : query.isError ? (
          <p className="text-pretty text-danger" role="alert" data-testid="run-credentials-error">
            {t("failed")}
          </p>
        ) : leases.length === 0 ? (
          <p className="text-pretty text-muted-foreground" data-testid="run-credentials-empty">
            {active ? t("emptyActive") : t("empty")}
          </p>
        ) : (
          <ul className="flex flex-col divide-y" data-testid="run-leases">
            {leases.map((lease) => (
              <LeaseItem key={lease.id} lease={lease} now={now} />
            ))}
          </ul>
        )}
        <p className="mt-3 border-t pt-3 text-xs text-fg-subtle">
          {t.rich("manage", {
            link: (chunks) => (
              <Link
                href={SECRETS_HREF}
                className="rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current"
                data-testid="run-credentials-secrets-link"
              >
                {chunks}
              </Link>
            ),
          })}
        </p>
      </div>
    </section>
  );
}
