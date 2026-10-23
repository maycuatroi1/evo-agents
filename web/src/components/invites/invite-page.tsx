"use client";

import { CircleCheck, Link2Off, TimerOff } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { HubMark } from "@/components/brand";
import { useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, LoadingState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { GetStarted } from "./get-started";
import { InviteSummary } from "./invite-summary";
import { invitePreviewQuery, type InvitePreview } from "./queries";

type Props = {
  token: string;
  /** The hub's origin as the server saw the request; the browser's own replaces it once hydrated. */
  origin: string;
  hubName: string;
  logoUrl: string | null;
  /** Whether the hub signs people in with GitHub now (GET /v1/auth/config). */
  loginReady: boolean;
  initialError: ApiErrorInfo | null;
  pairing: ReactNode;
};

const subscribe = () => () => {};

/** The hub's URL for the CLI: the web's origin, which serves /v1 too. */
function useHubUrl(serverOrigin: string): string {
  return useSyncExternalStore(subscribe, () => window.location.origin, () => serverOrigin);
}

type AcceptedProps = { invite: InvitePreview; hubUrl: string; pairing: ReactNode; justAccepted: boolean };

function Accepted({ invite, hubUrl, pairing, justAccepted }: AcceptedProps) {
  const t = useTranslations("invite");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (justAccepted) heading.current?.focus(); // the Accept button is gone: say what happened, from the top
  }, [justAccepted]);
  const tRoles = useTranslations("roles");
  const role = invite.role === "admin" || invite.role === "writer" || invite.role === "reader" ? tRoles(invite.role) : tRoles("none");
  const description =
    invite.kind === "join"
      ? t("acceptedJoin", { project: invite.project ?? "", role })
      : invite.project
        ? t("acceptedRegistered", { project: invite.project })
        : t("acceptedRegister");
  return (
    <div className="flex flex-col gap-8" data-testid="invite-accepted" data-kind={invite.kind}>
      <section aria-labelledby="invite-heading" className="flex items-start gap-3 rounded-md border bg-card p-5 shadow-raised sm:p-6">
        <span className="grid size-10 shrink-0 place-items-center rounded-md bg-success-soft text-success" aria-hidden="true">
          <CircleCheck className="size-5" />
        </span>
        <div className="flex min-w-0 flex-col gap-1">
          <h1 ref={heading} id="invite-heading" tabIndex={-1} className="text-xl font-semibold tracking-tight text-balance outline-none">
            {t("acceptedTitle")}
          </h1>
          <p className="text-sm text-pretty text-muted-foreground [overflow-wrap:anywhere]">{description}</p>
        </div>
      </section>
      <GetStarted invite={invite} hubUrl={hubUrl} pairing={pairing} />
    </div>
  );
}

function Closed({ invite }: { invite: InvitePreview }) {
  const t = useTranslations("invite");
  const status = invite.status === "open" ? "expired" : invite.status;
  return (
    <StatePanel
      icon={status === "expired" ? TimerOff : Link2Off}
      title={t(`closed.${status}`)}
      description={t("closedDescription", { inviter: invite.invited_by })}
      tone="warning"
      testId="invite-closed"
    >
      {invite.viewer ? (
        <Button asChild variant="outline">
          <Link href="/">{t("openHub")}</Link>
        </Button>
      ) : null}
    </StatePanel>
  );
}

function InviteSkeleton() {
  return (
    <LoadingState>
      <div className="flex flex-col gap-4 rounded-md border bg-card p-5 shadow-raised sm:p-8">
        <Skeleton className="h-7 w-3/4 rounded-xs" />
        <Skeleton className="h-4 w-1/2 rounded-xs" />
        <Skeleton className="h-4 w-2/3 rounded-xs" />
        <Skeleton className="mt-4 h-11 w-48 rounded-md" />
      </div>
    </LoadingState>
  );
}

/**
 * The public page of an invite link, outside the shell: the person it invites has no session yet. It reads the link's
 * public preview, and shows the link as open (to sign in and accept), closed, or accepted with the Get started list.
 */
export function InvitePage({ token, origin, hubName, logoUrl, loginReady, initialError, pairing }: Props) {
  const t = useTranslations("invite");
  const state = useHubQuery(invitePreviewQuery(browserApi, token), initialError);
  const hubUrl = useHubUrl(origin);
  const [justAccepted, setJustAccepted] = useState(false);

  let body: ReactNode;
  if (state.status === "loading") body = <InviteSkeleton />;
  else if (state.status === "error") {
    const unknown = state.error.status === 404 || state.error.status === 422;
    body = unknown ? (
      <StatePanel icon={Link2Off} title={t("unknownTitle")} description={t("unknownDescription")} testId="invite-unknown" />
    ) : (
      <ApiErrorState error={state.error} onRetry={state.retry} />
    );
  } else if (state.data.accepted_by_viewer) {
    body = <Accepted invite={state.data} hubUrl={hubUrl} pairing={pairing} justAccepted={justAccepted} />;
  } else if (state.data.status !== "open") {
    body = <Closed invite={state.data} />;
  } else {
    body = (
      <InviteSummary token={token} invite={state.data} hubName={hubName} loginReady={loginReady} onAccepted={() => setJustAccepted(true)} />
    );
  }

  return (
    <main id="main" className="min-h-svh bg-surface-sunken" data-testid="invite-page">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-8 px-4 py-8 sm:px-6 sm:py-12">
        <header className="flex min-w-0 items-center gap-3">
          <HubMark logoUrl={logoUrl} />
          <span className="truncate text-lg font-semibold tracking-tight">{hubName}</span>
        </header>
        {body}
      </div>
    </main>
  );
}
