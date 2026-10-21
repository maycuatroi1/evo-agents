"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, RefreshCw, UserRoundPlus, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

import { FailureBox } from "./hooks";
import { useTokenFailure, useWriteToken, type WriteToken } from "./login-hooks";
import { CheckList, CheckListSkeleton, code, EmptyPick, FieldError, Hint, KeptBindings, LoginCommand, Note, SetupSteps } from "./login-fields";
import {
  emptyLoginForm,
  LOGIN_FIELDS,
  type LoginField,
  type LoginForm,
  loginBody,
  loginCommand,
  loginProblems,
  type LoginProblems,
  nameMatch,
  renewForm,
  utcDay,
} from "./login-model";
import { MAX_SECRET_BYTES, MAX_SECRET_NAME_CHARS, secretNamesQuery, workersQuery } from "./login-queries";
import { OAuthRenewBody } from "./oauth-renew";
import { type Account, CLAUDE_TOKEN_VAR } from "./queries";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Null to add a Claude account; an account to give it a new token, its name and bindings kept. */
  account: Account | null;
  /** The visitor's accounts: a name among them gets a new token instead of a new account. */
  accounts: readonly Account[];
};

/**
 * Add Claude account and Renew token: a name, the projects and workers whose runs may take the account, the day its
 * token ends (a year less a day by default), and the token of `claude setup-token`, written as the env secret
 * CLAUDE_CODE_OAUTH_TOKEN with PUT /v1/secrets/{name}. A renewal keeps the name and the bindings and changes the token
 * and its end only. The token field is a password field the page never fills, read when the form is sent and emptied
 * at that moment, before the hub answers; the token is in no state, no log and no address. Below the form, the
 * `evo-agents hub account login` command does the same from a terminal, without a token to paste.
 */
export function LoginDialog({ open, onOpenChange, account, accounts }: Props) {
  const write = useWriteToken();
  const { reset } = write;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);
  const guard = (event: Event) => {
    if (write.pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (write.pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="login-dialog"
      >
        {account?.auth === "oauth" ? (
          <OAuthRenewBody account={account} />
        ) : (
          <LoginFormBody account={account} accounts={accounts} write={write} onClose={() => onOpenChange(false)} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function LoginFormBody({
  account,
  accounts,
  write,
  onClose,
}: {
  account: Account | null;
  accounts: readonly Account[];
  write: WriteToken;
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
  const { data: me } = useQuery({ ...whoamiQuery(browserApi), enabled: !renewing });
  const projects = useQuery({ ...projectsQuery(browserApi), enabled: !renewing });
  const workers = useQuery({ ...workersQuery(browserApi), enabled: !renewing });
  const secrets = useQuery({ ...secretNamesQuery(browserApi), enabled: !renewing });
  const [form, setForm] = useState<LoginForm>(() => (account ? renewForm(account, now) : emptyLoginForm(now)));
  const [errors, setErrors] = useState<LoginProblems>({});
  const [forgotten, setForgotten] = useState(false);
  const tokenRef = useRef<HTMLInputElement>(null);
  const pending = write.pending;

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
  const workerChoices = useMemo(
    () =>
      (workers.data ?? [])
        .filter((worker) => worker.owner === me?.login && worker.status !== "revoked")
        .map((worker) => worker.name)
        .sort((a, b) => a.localeCompare(b)),
    [workers.data, me?.login],
  );
  const noProjects = !renewing && !projects.isPending && !projects.isError && projectChoices.length === 0;
  const command = loginCommand(form, { renewing, now });
  const [minDay] = useState(() => utcDay(now + 24 * 60 * 60 * 1000));

  const set = (patch: Partial<LoginForm>, field?: LoginField) => {
    setForm((current) => ({ ...current, ...patch }));
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };
  const toggle = (list: "projects" | "workers", name: string, on: boolean) =>
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
    // The button goes with the Add form; the token field, which stays, takes the focus.
    tokenRef.current?.focus();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const field = tokenRef.current;
    const found = loginProblems(form, field?.value ?? "", { renewing, match, now: Date.now() });
    setErrors(found);
    const first = LOGIN_FIELDS.find((key) => found[key]);
    if (first) {
      document.getElementById(first === "projects" ? `${ids}-project-0` : `${ids}-${first}`)?.focus();
      return;
    }
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
    const until = format.dateTime(new Date(body.expires_at ?? now), { dateStyle: "medium", timeZone: "UTC" });
    notify(
      written.created
        ? { tone: "success", text: t("added", { name }), description: t("addedText", { until }) }
        : { tone: "success", text: t("renewed", { name }), description: t("renewedText", { until }) },
    );
    onClose();
  };

  const failure = write.error ? failureOf(write.error) : null;
  const TitleIcon = renewing ? RefreshCw : UserRoundPlus;

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold break-words">
            <TitleIcon className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">{renewing ? t("renewTitle", { name: target.name }) : t("addTitle")}</span>
          </DialogTitle>
          <DialogDescription className="text-pretty">
            {t.rich(renewing ? "renewDescription" : "addDescription", { code, variable: CLAUDE_TOKEN_VAR })}
          </DialogDescription>
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
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
          <Input
            id={`${ids}-name`}
            name="name"
            value={renewing ? target.name : form.name}
            onChange={(event) => set({ name: event.target.value }, "name")}
            readOnly={renewing}
            placeholder={t("namePlaceholder")}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={MAX_SECRET_NAME_CHARS}
            className="h-9 font-mono placeholder:font-sans read-only:bg-muted read-only:text-muted-foreground"
            aria-invalid={errors.name ? true : undefined}
            aria-describedby={describedBy("name")}
            aria-required={renewing ? undefined : "true"}
            data-testid="login-name"
          />
          <Hint id={`${ids}-name-hint`}>{renewing ? t("nameLocked") : t("nameHint")}</Hint>
          <FieldError id={`${ids}-name-error`} text={problem("name")} testId="login-name-error" />
          {match.kind === "account" ? (
            <Note tone="attention" icon={RefreshCw} text={t("nameTaken", { name: match.account.name })} live testId="login-name-taken">
              <Button type="button" variant="outline" size="sm" onClick={() => renewInstead(match.account)} data-testid="login-renew-instead">
                {t("renewInstead", { name: match.account.name })}
              </Button>
            </Note>
          ) : null}
        </div>

        {renewing ? (
          <div className="flex flex-col gap-1.5">
            <KeptBindings projects={target.projects} workers={target.workers} />
            <Hint id={`${ids}-kept-hint`}>{t("keptHint")}</Hint>
          </div>
        ) : (
          <>
            <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy("projects")}>
              <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
              {projects.isPending ? (
                <CheckListSkeleton />
              ) : projects.isError ? (
                <p className="text-sm text-danger" role="alert">
                  {t("projectsFailed")}
                </p>
              ) : noProjects ? (
                <EmptyPick testId="login-no-projects">{t("noProjects")}</EmptyPick>
              ) : (
                <CheckList
                  idPrefix={`${ids}-project`}
                  names={projectChoices}
                  picked={form.projects}
                  onToggle={(name, on) => toggle("projects", name, on)}
                  invalid={Boolean(errors.projects)}
                  testId="login-projects"
                />
              )}
              <Hint id={`${ids}-projects-hint`}>{t("projectsHint")}</Hint>
              <FieldError id={`${ids}-projects-error`} text={problem("projects")} testId="login-projects-error" />
            </fieldset>

            <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={`${ids}-workers-hint`}>
              <legend className="mb-1.5 text-sm font-medium">{t("workersOptional")}</legend>
              {workers.isPending ? (
                <CheckListSkeleton />
              ) : workerChoices.length === 0 ? (
                <EmptyPick testId="login-no-workers">{t("noWorkers")}</EmptyPick>
              ) : (
                <CheckList
                  idPrefix={`${ids}-worker`}
                  names={workerChoices}
                  picked={form.workers}
                  onToggle={(name, on) => toggle("workers", name, on)}
                  testId="login-workers"
                />
              )}
              <Hint id={`${ids}-workers-hint`}>{t("workersHint")}</Hint>
            </fieldset>
          </>
        )}

        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-expires`}>{t("expires")}</Label>
          <Input
            id={`${ids}-expires`}
            name="expires_at"
            type="date"
            min={minDay}
            value={form.expires}
            onChange={(event) => set({ expires: event.target.value }, "expires")}
            className="h-9 w-full tabular-nums sm:w-48"
            aria-invalid={errors.expires ? true : undefined}
            aria-describedby={describedBy("expires")}
            aria-required="true"
            data-testid="login-expires"
          />
          <Hint id={`${ids}-expires-hint`}>{t("expiresHint")}</Hint>
          <FieldError id={`${ids}-expires-error`} text={problem("expires")} testId="login-expires-error" />
        </div>

        <div className="flex flex-col gap-3 rounded-md border px-3 py-3">
          <h3 className="text-sm font-medium">{t("stepsTitle")}</h3>
          <SetupSteps />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-token`}>{t("token")}</Label>
            {/* No name attribute: the token is never part of a form submission the browser could make by itself. */}
            <Input
              ref={tokenRef}
              id={`${ids}-token`}
              type="password"
              defaultValue=""
              onChange={() => {
                setErrors((current) => ({ ...current, token: undefined }));
                setForgotten(false);
              }}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              className="h-9 font-mono"
              aria-invalid={errors.token ? true : undefined}
              aria-describedby={describedBy("token")}
              aria-required="true"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              data-form-type="other"
              data-testid="login-token"
            />
            <Hint id={`${ids}-token-hint`}>{t("tokenHint")}</Hint>
            <FieldError id={`${ids}-token-error`} text={problem("token")} testId="login-token-error" />
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-medium">{t("commandTitle")}</h3>
          <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground">
            {t.rich(renewing ? "commandRenew" : "commandAdd", { code })}
          </p>
          <LoginCommand command={command.text} complete={command.complete} />
          {command.complete ? null : <Hint id={`${ids}-command-hint`}>{t("commandIncomplete")}</Hint>}
        </div>

        {failure ? (
          <div className="flex flex-col gap-2">
            <FailureBox failure={failure} requestLabel={t("requestId", { id: failure.requestId ?? "" })} />
            {forgotten ? <Note tone="info" icon={Info} text={t("tokenForgotten")} testId="login-token-forgotten" /> : null}
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
          <Button type="submit" size="lg" disabled={noProjects} busy={pending} data-testid="login-save">
            <TitleIcon aria-hidden="true" />
            {pending ? t("saving") : renewing ? t("renew") : t("add")}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
