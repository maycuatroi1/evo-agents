"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleOff, Globe, LogIn, MoveRight, Plus, Power, PowerOff, Trash2, UserX } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { StatusPill, type StatusLook } from "@/components/status/status-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { useQuery } from "@tanstack/react-query";
import { whoamiQuery } from "@/lib/queries";

import { MoveDialog, NewIdentityDialog } from "./identity-forms";
import { canLogin } from "./model";
import {
  type BrowserIdentity,
  type BrowserIdentityCreate,
  createIdentity,
  deleteIdentity,
  disableIdentity,
  enableIdentity,
  identitiesQuery,
  identityKeys,
  loginIdentity,
  runHref,
} from "./queries";

/** The two states the list says: an identity that is active says nothing. */
const LOOKS: Record<"needs_login" | "disabled", StatusLook> = {
  needs_login: { tone: "attention", icon: UserX },
  disabled: { tone: "neutral", icon: CircleOff },
};

type Pending = { kind: "login" | "move" | "disable" | "enable" | "delete"; identity: BrowserIdentity };

/** One write on an identity, said as a toast; a failure inside a dialog is said there. */
function useIdentityWrite<TArgs, TResult>(project: string, write: (args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: identityKeys.all(project) }),
  });
}

function Fact({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm [overflow-wrap:anywhere]" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

function IdentityCard({ identity, highlighted, onAct }: { identity: BrowserIdentity; highlighted: boolean; onAct: (pending: Pending) => void }) {
  const t = useTranslations("browser");
  const status = identity.status;
  const loginRef = useRef<HTMLButtonElement>(null);
  const ready = canLogin(identity);
  useEffect(() => {
    if (highlighted) loginRef.current?.focus();
  }, [highlighted]);
  const act = (kind: Pending["kind"]) => () => onAct({ kind, identity });
  return (
    <li
      className={`flex min-w-0 flex-col gap-3 rounded-md border bg-card p-4 ${highlighted ? "ring-2 ring-ring" : ""}`}
      data-testid="browser-identity"
      data-identity={identity.name}
      data-status={status}
      data-highlighted={highlighted || undefined}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="min-w-0 text-base font-semibold [overflow-wrap:anywhere]">{identity.name}</h2>
        {status === "needs_login" || status === "disabled" ? <StatusPill look={LOOKS[status]} label={t(`status.${status}`)} data-testid="browser-status" /> : null}
        <span className="text-xs text-muted-foreground">{identity.owner === "project" ? t("ownerProject") : t("ownerMember", { login: identity.owner_login ?? "" })}</span>
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        <Fact label={t("fact.domains")} testId="browser-domains">
          {identity.domains.join(", ")}
        </Fact>
        <Fact label={t("fact.worker")}>{identity.worker}</Fact>
        <Fact label={t("fact.version")}>{identity.version === 0 ? t("neverSaved") : `v${identity.version}`}</Fact>
        <Fact label={t("fact.lastSaved")}><When value={identity.last_saved_at} never={t("none")} /></Fact>
        <Fact label={t("fact.lastUsed")}><When value={identity.last_used_at} never={t("none")} /></Fact>
        {identity.held_by_run !== null ? <Fact label={t("fact.heldBy")}>{t("heldBy", { id: identity.held_by_run })}</Fact> : null}
      </dl>
      <div className="flex flex-wrap items-center gap-2">
        <Button ref={loginRef} type="button" size="sm" disabled={!ready} onClick={act("login")} className="max-md:min-h-11" data-testid="browser-login">
          <LogIn aria-hidden="true" />
          {t("action.login")}
        </Button>
        {identity.can_manage ? (
          <>
            <Button type="button" size="sm" variant="outline" disabled={!ready} onClick={act("move")} className="max-md:min-h-11" data-testid="browser-move">
              <MoveRight aria-hidden="true" />
              {t("action.move")}
            </Button>
            {status === "disabled" ? (
              <Button type="button" size="sm" variant="outline" onClick={act("enable")} className="max-md:min-h-11" data-testid="browser-enable">
                <Power aria-hidden="true" />
                {t("action.enable")}
              </Button>
            ) : (
              <Button type="button" size="sm" variant="outline" onClick={act("disable")} className="max-md:min-h-11" data-testid="browser-disable">
                <PowerOff aria-hidden="true" />
                {t("action.disable")}
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" onClick={act("delete")} className="text-danger max-md:min-h-11" data-testid="browser-delete">
              <Trash2 aria-hidden="true" />
              {t("action.delete")}
            </Button>
          </>
        ) : null}
      </div>
    </li>
  );
}

/**
 * A project's browser identities the visitor may use: who owns each, its domains, worker, version and when it was last
 * saved and used. A pill appears only for an identity that needs a login or is disabled. Log in queues a login session
 * and opens its run, where the browser shows in human mode and Save keeps the profile; Move does the same on another
 * worker, and the identity moves there after Save. `login` is the identity a notice's link names.
 */
export function IdentitiesPage({ project, login, initialError }: { project: string; login: number | null; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("browser");
  const router = useRouter();
  const failure = useWriteFailure();
  const state = useHubQuery(identitiesQuery(browserApi, project), initialError, { live: true });
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const admin = Boolean(me?.admin) || me?.grants.some((grant) => grant.project === project && grant.role === "admin") === true;
  const [creating, setCreating] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);

  const close = () => {
    setPending(null);
    setError(null);
  };
  const toast = (text: string, description?: string) => notify({ tone: "success", text, description: description ?? null });

  const create = useIdentityWrite(project, (body: BrowserIdentityCreate) => createIdentity(browserApi(), project, body));
  const start = useIdentityWrite(project, ({ id, worker }: { id: number; worker: string | null }) => loginIdentity(browserApi(), project, id, worker));
  const switchTo = useIdentityWrite(project, ({ id, on }: { id: number; on: boolean }) => (on ? enableIdentity(browserApi(), project, id) : disableIdentity(browserApi(), project, id)));
  const remove = useIdentityWrite(project, (id: number) => deleteIdentity(browserApi(), project, id));

  const login_ = (identity: BrowserIdentity, worker: string | null) => {
    setError(null);
    start.mutate(
      { id: identity.id, worker },
      {
        onSuccess: (run) => {
          close();
          router.push(runHref(project, run.id));
        },
        onError: (cause) => setError(failure(cause, { 409: t("error.conflict") })),
      },
    );
  };

  const confirm = (action: Pending) => {
    setError(null);
    const { identity } = action;
    if (action.kind === "delete") {
      remove.mutate(identity.id, {
        onSuccess: () => {
          close();
          toast(t("toast.deleted", { name: identity.name }));
        },
        onError: (cause) => setError(failure(cause, { 409: t("error.conflict") })),
      });
    } else if (action.kind === "disable" || action.kind === "enable") {
      switchTo.mutate(
        { id: identity.id, on: action.kind === "enable" },
        {
          onSuccess: () => {
            close();
            toast(t(action.kind === "enable" ? "toast.enabled" : "toast.disabled", { name: identity.name }));
          },
          onError: (cause) => setError(failure(cause, { 409: t("error.conflict") })),
        },
      );
    }
  };

  const onAct = (action: Pending) => {
    setError(null);
    if (action.kind === "login") login_(action.identity, null);
    else if (action.kind === "enable") confirm(action);
    else setPending(action);
  };

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="browser-page">
      <PageHeader
        title={t("title")}
        actions={
          <Button type="button" size="sm" onClick={() => setCreating(true)} className="max-md:min-h-11" data-testid="browser-new">
            <Plus aria-hidden="true" />
            {t("new")}
          </Button>
        }
      />
      <QueryView state={state} loading={<ListSkeleton rows={3} />}>
        {(identities) =>
          identities.length === 0 ? (
            <EmptyState icon={Globe} title={t("empty.title")} description={<p>{t("empty.description")}</p>} />
          ) : (
            <ul className="flex flex-col gap-3" data-testid="browser-identities">
              {identities.map((identity) => (
                <IdentityCard key={identity.id} identity={identity} highlighted={login === identity.id} onAct={onAct} />
              ))}
            </ul>
          )
        }
      </QueryView>
      <NewIdentityDialog
        project={project}
        open={creating}
        onOpenChange={(open) => {
          setCreating(open);
          if (!open) setError(null);
        }}
        pending={create.isPending}
        error={creating ? error : null}
        canProject={admin}
        onCreate={(body) => {
          setError(null);
          create.mutate(body, {
            onSuccess: (identity) => {
              setCreating(false);
              toast(t("toast.created", { name: identity.name }), t("toast.createdNext"));
            },
            onError: (cause) => setError(failure(cause, { 409: t("error.exists") })),
          });
        }}
      />
      {pending?.kind === "move" ? (
        <MoveDialog
          project={project}
          name={pending.identity.name}
          current={pending.identity.worker}
          open
          onOpenChange={(open) => !open && close()}
          pending={start.isPending}
          error={error}
          onMove={(worker) => login_(pending.identity, worker)}
        />
      ) : null}
      {pending && (pending.kind === "delete" || pending.kind === "disable") ? (
        <ConfirmAction
          open
          onOpenChange={(open) => !open && close()}
          title={t(`confirm.${pending.kind}.title`, { name: pending.identity.name })}
          description={<p>{t(`confirm.${pending.kind}.description`)}</p>}
          confirmLabel={t(`confirm.${pending.kind}.confirm`)}
          pendingLabel={t(`confirm.${pending.kind}.pending`)}
          cancelLabel={t("confirm.cancel")}
          pending={remove.isPending || switchTo.isPending}
          error={error}
          onConfirm={() => confirm(pending)}
          testId={`browser-${pending.kind}-dialog`}
        />
      ) : null}
    </div>
  );
}

