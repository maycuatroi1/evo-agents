"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, RefreshCw, UserRoundPlus, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

import { ClaudeSignInSteps } from "./claude-sign-in-steps";
import { FailureBox } from "./hooks";
import { useTokenFailure, useWriteToken, type WriteToken } from "./login-hooks";
import { KeptBindings, Note } from "./login-fields";
import {
  emptyLoginForm,
  LOGIN_FIELDS,
  type LoginField,
  type LoginForm,
  loginBody,
  loginProblems,
  type LoginProblems,
  nameMatch,
  renewForm,
  signInProblems,
  suggestedName,
} from "./login-model";
import { MAX_SECRET_BYTES, poolsQuery, secretNamesQuery, workersQuery } from "./login-queries";
import { BindingsDisclosure, type Method, MethodSwitch, NameField, ProjectsPick, TokenMethod } from "./login-sections";
import type { Account } from "./queries";
import { useFinishSignIn, useSignInFlow } from "./sign-in-queries";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Null to add a Claude account; an account to give it a new token, its name and bindings kept. */
  account: Account | null;
  /** The visitor's accounts: a name among them gets a new token instead of a new account. */
  accounts: readonly Account[];
};

/**
 * Add Claude account and Renew token. The default is Claude's sign-in from the page: open claude.ai, approve, paste the
 * code it shows; the hub trades the code for the token of `claude setup-token` and keeps it (`sign-in-queries.ts`).
 * The other way is the token of `claude setup-token` run in a terminal, pasted in a password field the page never fills
 * and empties as the form is sent, with the `evo-agents hub account login` command that does the same without a paste.
 * A new account takes a name (one is suggested), the projects whose runs may take it and, folded, the workers or pools
 * it is kept to; a renewal keeps the name and the bindings. No token is ever in a state, a log or an address.
 */
export function LoginDialog({ open, onOpenChange, account, accounts }: Props) {
  const write = useWriteToken();
  const finish = useFinishSignIn(browserApi);
  const { reset } = write;
  const { reset: resetFinish } = finish;
  useEffect(() => {
    if (open) {
      reset();
      resetFinish();
    }
  }, [open, reset, resetFinish]);
  const pending = write.pending || finish.pending;
  // Each opening is a new form: a new name, a new sign-in, nothing typed before.
  const [opening, setOpening] = useState(0);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setOpening(opening + 1);
  }
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="login-dialog"
      >
        <LoginFormBody
          key={opening}
          opening={opening}
          active={open}
          account={account}
          accounts={accounts}
          write={write}
          finish={finish}
          onClose={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}


function LoginFormBody({
  opening,
  active,
  account,
  accounts,
  write,
  finish,
  onClose,
}: {
  opening: number;
  active: boolean;
  account: Account | null;
  accounts: readonly Account[];
  write: WriteToken;
  finish: ReturnType<typeof useFinishSignIn>;
  onClose: () => void;
}) {
  const t = useTranslations("accounts.login");
  const tProblem = useTranslations("accounts.login.problems");
  const format = useFormatter();
  const failureOf = useTokenFailure();
  const ids = useId();
  const [now] = useState(() => Date.now());
  const [target, setTarget] = useState<Account | null>(account);
  const renewing = target !== null;
  const [method, setMethod] = useState<Method>("web");
  const sign = useSignInFlow(browserApi, opening, active && method === "web");
  const { data: me } = useQuery({ ...whoamiQuery(browserApi), enabled: !renewing });
  const projects = useQuery({ ...projectsQuery(browserApi), enabled: !renewing });
  const workers = useQuery({ ...workersQuery(browserApi), enabled: !renewing });
  const pools = useQuery({ ...poolsQuery(browserApi), enabled: !renewing });
  const secrets = useQuery({ ...secretNamesQuery(browserApi), enabled: !renewing });
  const [form, setForm] = useState<LoginForm>(() =>
    account ? renewForm(account, now) : { ...emptyLoginForm(now), name: suggestedName(accounts.map((item) => item.name)) },
  );
  const [errors, setErrors] = useState<LoginProblems>({});
  const [forgotten, setForgotten] = useState(false);
  const tokenRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const pending = write.pending || finish.pending;

  const match = useMemo(
    () => (renewing ? ({ kind: "free" } as const) : nameMatch(form.name, accounts, secrets.data ?? [])),
    [renewing, form.name, accounts, secrets.data],
  );
  const projectChoices = useMemo(
    () =>
      (projects.data ?? [])
        .filter((project) => project.role === "writer" || project.role === "admin")
        .map((project) => project.name)
        .sort(),
    [projects.data],
  );
  // The one project a member may bind is picked for them.
  const onlyProject = projectChoices.length === 1 ? projectChoices[0] : null;
  const [autoPicked, setAutoPicked] = useState(false);
  if (onlyProject && !autoPicked) {
    setAutoPicked(true);
    if (form.projects.length === 0) setForm({ ...form, projects: [onlyProject] });
  }
  const workerChoices = useMemo(
    () =>
      (workers.data ?? [])
        .filter((worker) => worker.owner === me?.login && worker.status !== "revoked")
        .map((worker) => worker.name)
        .sort((a, b) => a.localeCompare(b)),
    [workers.data, me?.login],
  );
  const poolChoices = useMemo(
    () => (pools.data ?? []).filter((pool) => pool.owner === me?.login).map((pool) => pool.name).sort((a, b) => a.localeCompare(b)),
    [pools.data, me?.login],
  );
  const noProjects = !renewing && !projects.isPending && !projects.isError && projectChoices.length === 0;

  const set = (patch: Partial<LoginForm>, field?: LoginField) => {
    setForm((current) => ({ ...current, ...patch }));
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };
  const toggle = (list: "projects" | "workers" | "pools", name: string, on: boolean) =>
    set({ [list]: on ? [...form[list], name] : form[list].filter((item) => item !== name) }, list === "projects" ? "projects" : undefined);
  const describedBy = (field: LoginField) => [`${ids}-${field}-hint`, errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ");
  const problem = (field: LoginField) => {
    const found = errors[field];
    if (!found) return null;
    if (found === "tokenTooLong") return tProblem(found, { max: MAX_SECRET_BYTES });
    if (found === "nameOther") return tProblem(found, { name: form.name });
    return tProblem(found);
  };
  /** The account the typed name already is becomes the one renewed: its bindings, the end day chosen, no new account. */
  const renewInstead = (existing: Account) => {
    setTarget(existing);
    setForm((current) => ({ ...renewForm(existing, now), expires: current.expires }));
    setErrors({});
    (method === "web" ? codeRef : tokenRef).current?.focus();
  };
  const focusFirst = (found: LoginProblems) => {
    const first = LOGIN_FIELDS.find((key) => found[key]);
    if (!first) return false;
    const id = first === "projects" ? `${ids}-project-0` : first === "code" ? `${ids}-code` : `${ids}-${first}`;
    document.getElementById(id)?.focus();
    return true;
  };
  const until = (iso: string | null | undefined) => format.dateTime(new Date(iso ?? now), { dateStyle: "medium", timeZone: "UTC" });

  const submitWeb = async () => {
    const field = codeRef.current;
    const found = signInProblems(form, field?.value ?? "", { renewing, match });
    setErrors(found);
    if (focusFirst(found)) return;
    const started = sign.current();
    if (!started) return;
    const name = target?.name ?? form.name;
    const bindings = renewing ? {} : { projects: [...form.projects], workers: [...form.workers], pools: [...form.pools] };
    const { written, status } = await finish.finish({ flow: started.flow, code: (field?.value ?? "").trim(), name, ...bindings });
    if (!written) {
      // A refused, spent or stale sign-in starts again; a refusal of the name or the projects keeps the code.
      if (field && status !== 403 && status !== 404 && status !== 409) {
        field.value = "";
        sign.restart();
      }
      return;
    }
    const email = written.email ? t("signedInAs", { email: written.email }) : "";
    notify(
      written.created
        ? { tone: "success", text: t("added", { name }), description: `${email}${t("addedWebText", { until: until(written.expires_at) })}` }
        : { tone: "success", text: t("renewed", { name }), description: `${email}${t("renewedText", { until: until(written.expires_at) })}` },
    );
    onClose();
  };

  const submitToken = async () => {
    const field = tokenRef.current;
    const found = loginProblems(form, field?.value ?? "", { renewing, match, now: Date.now() });
    setErrors(found);
    if (focusFirst(found)) return;
    const name = target?.name ?? form.name;
    const body = loginBody(form, field?.value ?? "");
    // The token leaves the page with this request and is not kept: the field is emptied before the hub answers.
    if (field) field.value = "";
    setForgotten(false);
    const written = await write.put(name, body);
    if (!written) {
      setForgotten(true);
      return; // the reason shows above the buttons, from the write's state
    }
    notify(
      written.created
        ? { tone: "success", text: t("added", { name }), description: t("addedText", { until: until(body.expires_at) }) }
        : { tone: "success", text: t("renewed", { name }), description: t("renewedText", { until: until(body.expires_at) }) },
    );
    onClose();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    await (method === "web" ? submitWeb() : submitToken());
  };

  const failing = method === "web" ? finish.error : write.error;
  const failure = failing ? failureOf(failing) : null;
  const TitleIcon = renewing ? RefreshCw : UserRoundPlus;
  const webReady = method === "token" || Boolean(sign.flow.data);

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold break-words">
            <TitleIcon className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">{renewing ? t("renewTitle", { name: target.name }) : t("addTitle")}</span>
          </DialogTitle>
          <DialogDescription className="text-pretty">{renewing ? t("renewDescription") : t("addDescription")}</DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={t("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </DialogHeader>

      <form
        onSubmit={(event) => void submit(event)}
        noValidate
        className="flex flex-col gap-5"
        aria-busy={pending || undefined}
        data-testid="login-form"
        data-mode={renewing ? "renew" : "add"}
        data-method={method}
      >
        {renewing ? (
          <KeptBindings projects={target.projects} workers={target.workers} pools={target.pools} />
        ) : (
          <>
            <NameField
              ids={ids}
              value={form.name}
              invalid={Boolean(errors.name)}
              problem={problem("name")}
              describedBy={describedBy("name")}
              taken={match.kind === "account" ? match.account : null}
              onChange={(name) => set({ name }, "name")}
              onRenewInstead={renewInstead}
            />

            <ProjectsPick
              ids={ids}
              state={projects.isPending ? "loading" : projects.isError ? "failed" : noProjects ? "none" : "ready"}
              names={projectChoices}
              picked={form.projects}
              invalid={Boolean(errors.projects)}
              problem={problem("projects")}
              describedBy={describedBy("projects")}
              onToggle={(name, on) => toggle("projects", name, on)}
            />

            <BindingsDisclosure
              idPrefix={ids}
              form={form}
              loading={{ workers: workers.isPending, pools: pools.isPending }}
              choices={{ workers: workerChoices, pools: poolChoices }}
              onToggle={(list, name, on) => toggle(list, name, on)}
            />
          </>
        )}

        <section className="flex flex-col gap-4" aria-label={t("connectTitle")}>
          <MethodSwitch
            method={method}
            onChange={(next) => {
              setMethod(next);
              setErrors({});
            }}
          />
          {method === "web" ? (
            <ClaudeSignInSteps
              sign={sign}
              codeRef={codeRef}
              codeId={`${ids}-code`}
              codeProblem={problem("code")}
              onCodeChange={() => setErrors((current) => ({ ...current, code: undefined }))}
            />
          ) : (
            <TokenMethod
              ids={ids}
              tokenRef={tokenRef}
              form={form}
              renewing={renewing}
              now={now}
              errors={errors}
              problem={problem}
              describedBy={describedBy}
              onExpires={(expires) => set({ expires }, "expires")}
              onTokenChange={() => {
                setErrors((current) => ({ ...current, token: undefined }));
                setForgotten(false);
              }}
            />
          )}
        </section>

        {failure ? (
          <div className="flex flex-col gap-2">
            <FailureBox failure={failure} requestLabel={t("requestId", { id: failure.requestId ?? "" })} />
            {forgotten && method === "token" ? <Note tone="info" icon={Info} text={t("tokenForgotten")} testId="login-token-forgotten" /> : null}
          </div>
        ) : null}
        <DialogFooter>
          <DialogClose asChild>
            <Button
              type="button"
              variant="outline"
              size="lg"
              aria-disabled={pending || undefined}
              className="aria-disabled:opacity-50"
              onClick={(event) => pending && event.preventDefault()}
            >
              {t("cancel")}
            </Button>
          </DialogClose>
          <Button type="submit" size="lg" disabled={noProjects || !webReady} busy={pending} data-testid="login-save">
            <TitleIcon aria-hidden="true" />
            {pending ? t("saving") : renewing ? t("renew") : t("add")}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}

