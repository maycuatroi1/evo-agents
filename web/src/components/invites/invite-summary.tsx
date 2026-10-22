"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LogOut, TriangleAlert, UserCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { GitHubMark } from "@/components/brand";
import { When } from "@/components/data/when";
import { VisibilityLevel } from "@/components/data/visibility";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { RoleBadge } from "@/components/shell/role-badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { invitePath } from "@/lib/invite-path";
import { queryKeys } from "@/lib/queries";

import { acceptInvite, inviteKeys, type InvitePreview, signInHref, signOut } from "./queries";

type Props = {
  token: string;
  invite: InvitePreview;
  hubName: string;
  loginReady: boolean;
  /** Called once the hub took the accept, before the preview reloads to show it. */
  onAccepted: () => void;
};

/** What the link gives, as the preview shows it: the project and role only to someone the link is for. */
function Facts({ invite }: { invite: InvitePreview }) {
  const t = useTranslations("invite.facts");
  const tInvite = useTranslations("invite");
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-y-3" data-testid="invite-facts">
      <dt className="text-muted-foreground">{t("invitedBy")}</dt>
      <dd className="mb-2 min-w-0 font-mono font-medium break-all sm:mb-0" data-testid="invite-inviter">
        {invite.invited_by}
      </dd>
      {invite.kind === "register" ? (
        <>
          <dt className="text-muted-foreground">{t("gives")}</dt>
          <dd className="mb-2 font-medium text-pretty sm:mb-0" data-testid="invite-register">
            {tInvite("register")}
          </dd>
        </>
      ) : invite.project ? (
        <>
          <dt className="text-muted-foreground">{t("project")}</dt>
          <dd className="mb-2 min-w-0 font-mono font-medium break-all sm:mb-0" data-testid="invite-project">
            {invite.project}
          </dd>
          <dt className="text-muted-foreground">{t("role")}</dt>
          <dd className="mb-2 sm:mb-0" data-testid="invite-role">
            <RoleBadge role={invite.role} />
          </dd>
          <dt className="text-muted-foreground">{t("visibility")}</dt>
          <dd className="mb-2 font-medium sm:mb-0">
            <VisibilityLevel level={invite.max_level} testId="invite-level" />
          </dd>
        </>
      ) : (
        <>
          <dt className="text-muted-foreground">{t("gives")}</dt>
          <dd className="mb-2 text-pretty text-muted-foreground sm:mb-0" data-testid="invite-hidden">
            {tInvite("hidden")}
          </dd>
        </>
      )}
      <dt className="text-muted-foreground">{t("expires")}</dt>
      <dd className="font-medium">
        <When value={invite.expires_at} />
      </dd>
    </dl>
  );
}

/**
 * An open link: who invites, what it gives and until when, then the one action the visitor has. Signed out, the
 * GitHub button, which comes back to this page; signed in with a login the link is for, Accept; signed in with another
 * login, Sign out. Accepting reloads the preview, and the page then shows the Get started list.
 */
export function InviteSummary({ token, invite, hubName, loginReady, onAccepted }: Props) {
  const t = useTranslations("invite");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const inviter = invite.invited_by;

  const accept = useMutation({
    mutationFn: () => acceptInvite(browserApi(), token),
    onSuccess: onAccepted,
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(signInHref(token)); // session ended
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: inviteKeys.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
        queryClient.invalidateQueries({ queryKey: queryKeys.whoami }),
      ]),
  });
  const leave = useMutation({
    mutationFn: () => signOut(browserApi()),
    onSuccess: () => window.location.assign(invitePath(token)), // the same page, signed out
  });

  const acceptError = (error: unknown): WriteFailure => {
    if (isApiError(error) && error.status === 410) {
      return { text: t("errors.gone", { inviter }), detail: null, requestId: error.info.requestId, status: 410 };
    }
    return failure(error, {
      403: t("errors.forbidden"),
      404: t("errors.notFound", { inviter }),
      409: t("errors.conflict", { inviter }),
    });
  };
  const acceptFailed = accept.isError && !(isApiError(accept.error) && accept.error.kind === "unauthorized") ? acceptError(accept.error) : null;
  const leaveFailed = leave.isError ? failure(leave.error) : null;

  return (
    <section
      aria-labelledby="invite-heading"
      className="flex flex-col gap-6 rounded-md border bg-card p-5 shadow-raised sm:p-8"
      data-testid="invite-summary"
      data-state={invite.viewer === null ? "signed-out" : invite.for_viewer ? "for-viewer" : "other-login"}
    >
      <h1 id="invite-heading" className="text-xl font-semibold tracking-tight text-balance [overflow-wrap:anywhere]">
        {t("heading", { inviter, hub: hubName })}
      </h1>
      <Facts invite={invite} />

      <div className="flex flex-col gap-4 border-t pt-6">
        {invite.viewer === null ? (
          <>
            <p className="text-sm text-pretty text-muted-foreground">{t("signInHint")}</p>
            {loginReady ? (
              <Button asChild size="lg" className="h-11 w-full text-base sm:w-auto sm:self-start">
                <a href={signInHref(token)} data-testid="invite-sign-in">
                  <GitHubMark className="size-5" />
                  {t("github")}
                </a>
              </Button>
            ) : (
              <>
                <Alert variant="destructive" data-testid="invite-login-unavailable">
                  <TriangleAlert aria-hidden="true" />
                  <AlertDescription>{t("loginUnavailable", { inviter })}</AlertDescription>
                </Alert>
                <Button size="lg" className="h-11 w-full text-base sm:w-auto sm:self-start" disabled>
                  <GitHubMark className="size-5" />
                  {t("github")}
                </Button>
              </>
            )}
          </>
        ) : invite.for_viewer ? (
          <>
            <p className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="invite-viewer">
              <UserCheck className="size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-all">{t("signedInAs", { login: invite.viewer })}</span>
            </p>
            {acceptFailed ? <InlineError text={acceptFailed.text} detail={acceptFailed.detail} requestId={acceptFailed.requestId} /> : null}
            <Button
              type="button"
              size="lg"
              className="h-11 w-full text-base sm:w-auto sm:self-start"
              busy={accept.isPending}
              onClick={() => {
                if (!accept.isPending) accept.mutate();
              }}
              data-testid="invite-accept"
            >
              {accept.isPending ? t("accepting") : t("accept")}
            </Button>
          </>
        ) : (
          <>
            <Alert variant="destructive" data-testid="invite-not-for-you">
              <TriangleAlert aria-hidden="true" />
              <AlertDescription>{t("notForYou", { login: invite.viewer })}</AlertDescription>
            </Alert>
            {leaveFailed ? <InlineError text={t("signOutFailed")} detail={leaveFailed.text} requestId={leaveFailed.requestId} /> : null}
            <Button
              type="button"
              variant="outline"
              size="lg"
              className="w-full sm:w-auto sm:self-start"
              busy={leave.isPending}
              onClick={() => {
                if (!leave.isPending) leave.mutate();
              }}
              data-testid="invite-sign-out"
            >
              <LogOut aria-hidden="true" />
              {leave.isPending ? t("signingOut") : t("signOut")}
            </Button>
          </>
        )}
      </div>
    </section>
  );
}
