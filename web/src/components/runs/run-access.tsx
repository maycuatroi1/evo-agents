"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldOff, UserRoundKey } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import type { WriteFailure } from "@/components/feedback/notice";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";

import { useRunFailure } from "./hooks";
import { isActiveState, type Run, runKey, runKeys } from "./queries";
import { Card, Fact, When } from "./run-facts";

/**
 * A run that runs as its owner (docs/credentials.md, "A run that runs as its owner"): its agent calls the hub's API
 * with the owner's access, secrets, tokens and workers aside, through a token of the run's own that ends with the run.
 * The side column's card says so to everyone who reads the run, with who let it (the owner, the one member who may)
 * and since when; the owner alone gets Revoke while the run is active. Nothing for a run that does not run as its owner.
 */

/** PUT .../runs/{id}/access with the session's CSRF header: `as_me` false ends the run's token at once. */
export async function changeRunAccess(api: ApiClient, project: string, id: number, asMe: boolean): Promise<Run> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/runs/{run_id}/access", { params: { path: { project, run_id: id } }, body: { as_me: asMe }, headers }));
}

/** The owner's stop of a run's access; success and failure alike read the run, the project's runs and the plan again. */
export function useRevokeRunAccess(run: Pick<Run, "project" | "id" | "plan_id">) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => changeRunAccess(browserApi(), run.project, run.id, false),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSuccess: (answer) => queryClient.setQueryData(runKey(run.project, run.id), answer),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKey(run.project, run.id) }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(run.project) }),
        run.plan_id ? queryClient.invalidateQueries({ queryKey: planKeys.one(run.project, run.plan_id) }) : null,
      ]),
  });
}

/** "Runs as octo" while the run is active, "Ran as octo" once it ended: the card's state, and the run head's tag. */
export function AsOwnerBadge({ run, className }: { run: Pick<Run, "as_owner" | "dispatched_by" | "state">; className?: string }) {
  const t = useTranslations("runs.detail.access");
  if (!run.as_owner) return null;
  const active = isActiveState(run.state);
  return (
    <Badge variant={active ? "info" : "secondary"} className={className} data-testid="run-as-owner" data-active={active || undefined}>
      <UserRoundKey aria-hidden="true" />
      {t(active ? "active" : "ended", { login: run.dispatched_by })}
    </Badge>
  );
}

/** The card of the side column: what the run's agent may do as its owner, who let it and since when, and Revoke. */
export function RunAccess({ run, owner }: { run: Run; owner: boolean }) {
  const t = useTranslations("runs.detail.access");
  const failure = useRunFailure();
  const revoke = useRevokeRunAccess(run);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  if (!run.as_owner) return null;
  const active = isActiveState(run.state);
  const login = run.dispatched_by;

  const confirm = () =>
    revoke.mutate(undefined, {
      onSuccess: () => {
        setOpen(false);
        notify({ tone: "success", text: t("revoked", { id: run.id }), description: t("revokedText") });
      },
      onError: (caught) => setError(failure(caught)),
    });

  return (
    <Card title={t("title")} testId="run-access" status={<AsOwnerBadge run={run} className="ml-auto" />}>
      <div className="flex min-w-0 flex-col gap-3 text-sm">
        <p className="text-pretty text-muted-foreground" data-testid="run-access-text">
          {owner ? t("textOwner") : t("textOther", { login })}
        </p>
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2">
          <Fact label={t("grantedBy")} testId="run-access-by">
            <span className="font-mono text-xs">{login}</span>
          </Fact>
          <Fact label={t("since")} testId="run-access-since">
            {run.as_owner_since ? <When at={run.as_owner_since} /> : <span className="text-fg-subtle">{t("sinceUnknown")}</span>}
          </Fact>
          <Fact label={t("ends")} testId="run-access-ends">
            {active ? (owner ? t("endsOwner") : t("endsOther", { login })) : <span className="text-fg-subtle">{t("endsDone")}</span>}
          </Fact>
        </dl>
        {owner && active ? (
          <Button
            type="button"
            variant="quiet-danger"
            className="self-start max-md:h-11"
            aria-label={t("revokeLabel", { id: run.id })}
            onClick={() => {
              setError(null);
              setOpen(true);
            }}
            data-testid="run-access-revoke"
          >
            <ShieldOff aria-hidden="true" />
            {t("revoke")}
          </Button>
        ) : null}
      </div>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("confirmTitle", { id: run.id })}
        description={
          <>
            <p>{t("confirmText")}</p>
            <p>{t.rich("confirmAgain", { id: run.id, code: (chunks) => <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{chunks}</code> })}</p>
          </>
        }
        confirmLabel={t("confirm")}
        pendingLabel={t("pending")}
        cancelLabel={t("cancel")}
        pending={revoke.isPending}
        error={error}
        onConfirm={confirm}
        testId="run-access-dialog"
      />
    </Card>
  );
}
