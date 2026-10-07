"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, LockKeyhole, RefreshCw, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";

import { InlineError } from "@/components/admin/notice";
import type { Notice } from "@/components/feedback/toast";
import { Choice, Section } from "@/components/runs/dispatch-fields";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery, whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { type PutSecret, usePutSecret, useSecretFailure } from "./hooks";
import {
  emptySecretForm,
  formOf,
  SECRET_FIELDS,
  secretBody,
  type SecretField,
  type SecretForm,
  secretProblems,
  type SecretProblems,
  utcDay,
} from "./model";
import {
  DEFAULT_GIT_USERNAME,
  MAX_ENV_VAR_CHARS,
  MAX_SECRET_BYTES,
  MAX_SECRET_NAME_CHARS,
  MAX_URL_PREFIX_CHARS,
  MAX_USERNAME_CHARS,
  type Secret,
} from "./queries";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Null to add a secret; a secret to replace it whole, value included. */
  secret: Secret | null;
  /** The names of the visitor's secrets: adding one of them would replace it. */
  taken: string[];
  /** Called once the hub kept the secret, for the page to say so after the dialog closes. */
  onSaved: (notice: Notice) => void;
};

/**
 * Adding a secret, or replacing one whole. The value field is a password input the page never fills: the hub has no
 * copy it could show, so a replace asks for the value again. The value is read from the field when the form is sent,
 * and the field is emptied at that moment, before the hub answers, whatever it answers. The dialog's content mounts
 * each time it opens, so a new opening starts from the hub's state of the secret, or an empty form.
 */
export function SecretDialog({ open, onOpenChange, secret, taken, onSaved }: Props) {
  const write = usePutSecret();
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
        data-testid="secret-dialog"
        data-mode={secret ? "replace" : "add"}
      >
        <SecretFormBody secret={secret} taken={taken} write={write} onClose={() => onOpenChange(false)} onSaved={onSaved} />
      </DialogContent>
    </Dialog>
  );
}

function FieldError({ id, text, testId }: { id: string; text?: string | null; testId: string }) {
  if (!text) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger" data-testid={testId}>
      {text}
    </p>
  );
}

function Hint({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p id={id} className="text-xs text-pretty text-muted-foreground">
      {children}
    </p>
  );
}

function Box({ tone, icon: Icon, children, testId }: { tone: "info" | "warning"; icon: typeof Info; children: ReactNode; testId?: string }) {
  return (
    <p
      className={
        tone === "warning"
          ? "flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
          : "flex items-start gap-2.5 rounded-md border bg-muted px-3 py-2.5 text-sm text-foreground"
      }
      data-testid={testId}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="text-pretty">{children}</span>
    </p>
  );
}

/** Today's day after `now` in UTC: the first end a secret may have. */
function tomorrow(now: number): string {
  return utcDay(new Date(now + 24 * 60 * 60 * 1000).toISOString());
}

function SecretFormBody({
  secret,
  taken,
  write,
  onClose,
  onSaved,
}: {
  secret: Secret | null;
  taken: string[];
  write: PutSecret;
  onClose: () => void;
  onSaved: (notice: Notice) => void;
}) {
  const t = useTranslations("secrets.form");
  const tProblem = useTranslations("secrets.form.problems");
  const failure = useSecretFailure();
  const ids = useId();
  const adding = secret === null;
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const projects = useQuery(projectsQuery(browserApi));
  const workers = useQuery(workersQuery(browserApi));
  const [form, setForm] = useState<SecretForm>(() => (secret ? formOf(secret) : emptySecretForm()));
  const [errors, setErrors] = useState<SecretProblems>({});
  const [forgotten, setForgotten] = useState(false);
  const valueRef = useRef<HTMLInputElement>(null);
  const [minDay] = useState(() => tomorrow(Date.now()));
  const pending = write.pending;

  // A project or worker the secret names stays offered on a replace, even when the visitor no longer writes to that
  // project or the worker is gone: the hub then says why it refuses.
  const projectChoices = useMemo(() => {
    const writable = (projects.data ?? []).filter((project) => project.role === "writer" || project.role === "admin").map((project) => project.name);
    return [...new Set([...writable, ...(secret?.projects ?? [])])].sort();
  }, [projects.data, secret]);
  const workerChoices = useMemo(() => {
    const own = (workers.data ?? []).filter((worker) => worker.owner === me?.login && worker.status !== "revoked").map((worker) => worker.name);
    return [...new Set([...own, ...(secret?.workers ?? [])])].sort((a, b) => a.localeCompare(b));
  }, [workers.data, me?.login, secret]);
  const noProjects = !projects.isPending && !projects.isError && projectChoices.length === 0;

  const set = (patch: Partial<SecretForm>, field?: SecretField) => {
    setForm((current) => ({ ...current, ...patch }));
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };
  const toggle = (list: "projects" | "workers", name: string, on: boolean) =>
    set({ [list]: on ? [...form[list], name] : form[list].filter((item) => item !== name) }, list === "projects" ? "projects" : undefined);
  const describedBy = (field: SecretField, hint = true) =>
    [hint ? `${ids}-${field}-hint` : "", errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ") || undefined;
  const problem = (field: SecretField) => {
    const found = errors[field];
    if (!found) return null;
    return found === "valueTooLong" ? tProblem(found, { max: MAX_SECRET_BYTES }) : tProblem(found);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const field = valueRef.current;
    const value = field?.value ?? "";
    const found = secretProblems({ ...form, value }, { adding, taken, original: secret, now: Date.now() });
    setErrors(found);
    const first = SECRET_FIELDS.find((key) => found[key]);
    if (first) {
      const target = first === "projects" ? `${ids}-project-0` : `${ids}-${first}`;
      document.getElementById(target)?.focus();
      return;
    }
    const body = secretBody({ ...form, value }, secret);
    const name = secret?.name ?? form.name;
    // The value leaves the page with this request and is not kept: the field is emptied before the hub answers.
    if (field) field.value = "";
    setForgotten(false);
    const written = await write.put(name, body);
    if (!written) {
      setForgotten(true);
      return; // the reason shows above the buttons, from the write's state
    }
    onSaved(
      written.created
        ? { tone: "success", text: t("added", { name }), description: t("addedText") }
        : { tone: "success", text: t("replaced", { name }), description: t("replacedText") },
    );
    onClose();
  };

  const error = write.error ? failure(write.error) : null;
  const kindChoice = (kind: SecretForm["kind"]) => (
    <Choice
      type="radio"
      name={`${ids}-kind`}
      value={kind}
      checked={form.kind === kind}
      onChange={() => set({ kind })}
      title={t(`kinds.${kind}`)}
      hint={t(`kinds.${kind}Hint`)}
      testId={`secret-kind-${kind}`}
    />
  );

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold break-words">
            {adding ? (
              <LockKeyhole className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            ) : (
              <RefreshCw className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            )}
            <span className="min-w-0 [overflow-wrap:anywhere]">{adding ? t("addTitle") : t("replaceTitle", { name: secret.name })}</span>
          </DialogTitle>
          <DialogDescription>{adding ? t("addDescription") : t("replaceDescription")}</DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={t("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </DialogHeader>

      <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={pending || undefined} data-testid="secret-form">
        {adding ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
            <Input
              id={`${ids}-name`}
              name="name"
              value={form.name}
              onChange={(event) => set({ name: event.target.value }, "name")}
              placeholder={t("namePlaceholder")}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={MAX_SECRET_NAME_CHARS}
              className="h-9 font-mono placeholder:font-sans"
              aria-invalid={errors.name ? true : undefined}
              aria-describedby={describedBy("name")}
              aria-required="true"
              data-testid="secret-name"
            />
            <Hint id={`${ids}-name-hint`}>{t("nameHint")}</Hint>
            <FieldError id={`${ids}-name-error`} text={problem("name")} testId="secret-name-error" />
          </div>
        ) : null}

        <Section legend={t("kind")} testId="secret-kind">
          <div className="grid gap-2 sm:grid-cols-2">
            {kindChoice("env")}
            {kindChoice("git")}
          </div>
        </Section>

        {form.kind === "env" ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-envVar`}>{t("envVar")}</Label>
            <Input
              id={`${ids}-envVar`}
              name="env_var"
              value={form.envVar}
              onChange={(event) => set({ envVar: event.target.value }, "envVar")}
              placeholder="CLAUDE_CODE_OAUTH_TOKEN"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={MAX_ENV_VAR_CHARS}
              className="h-9 font-mono"
              aria-invalid={errors.envVar ? true : undefined}
              aria-describedby={describedBy("envVar")}
              aria-required="true"
              data-testid="secret-env-var"
            />
            <Hint id={`${ids}-envVar-hint`}>{t("envVarHint")}</Hint>
            <FieldError id={`${ids}-envVar-error`} text={problem("envVar")} testId="secret-env-var-error" />
          </div>
        ) : (
          <div className="grid gap-5 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="flex min-w-0 flex-col gap-1.5">
              <Label htmlFor={`${ids}-urlPrefix`}>{t("urlPrefix")}</Label>
              <Input
                id={`${ids}-urlPrefix`}
                name="url_prefix"
                type="url"
                inputMode="url"
                value={form.urlPrefix}
                onChange={(event) => set({ urlPrefix: event.target.value }, "urlPrefix")}
                placeholder="https://gitlab.example.org/group"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={MAX_URL_PREFIX_CHARS}
                className="h-9 font-mono"
                aria-invalid={errors.urlPrefix ? true : undefined}
                aria-describedby={describedBy("urlPrefix")}
                aria-required="true"
                data-testid="secret-url-prefix"
              />
              <Hint id={`${ids}-urlPrefix-hint`}>{t("urlPrefixHint")}</Hint>
              <FieldError id={`${ids}-urlPrefix-error`} text={problem("urlPrefix")} testId="secret-url-prefix-error" />
            </div>
            <div className="flex min-w-0 flex-col gap-1.5">
              <Label htmlFor={`${ids}-username`}>{t("username")}</Label>
              <Input
                id={`${ids}-username`}
                name="username"
                value={form.username}
                onChange={(event) => set({ username: event.target.value }, "username")}
                placeholder={DEFAULT_GIT_USERNAME}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={MAX_USERNAME_CHARS}
                className="h-9 font-mono"
                aria-invalid={errors.username ? true : undefined}
                aria-describedby={describedBy("username")}
                data-testid="secret-username"
              />
              <Hint id={`${ids}-username-hint`}>{t("usernameHint", { user: DEFAULT_GIT_USERNAME })}</Hint>
              <FieldError id={`${ids}-username-error`} text={problem("username")} testId="secret-username-error" />
            </div>
          </div>
        )}

        <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy("projects")} data-testid="secret-projects">
          <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
          {projects.isPending ? (
            <div className="grid gap-2 sm:grid-cols-2" aria-hidden="true">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : projects.isError ? (
            <p className="text-sm text-danger" role="alert">
              {t("projectsFailed")}
            </p>
          ) : noProjects ? (
            <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="secret-no-projects">
              {t("noProjects")}
            </p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {projectChoices.map((name, index) => (
                <label
                  key={name}
                  htmlFor={`${ids}-project-${index}`}
                  className={cn(
                    "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors hover:bg-muted/50",
                    form.projects.includes(name) && "border-brand/40 bg-surface-selected",
                  )}
                >
                  <input
                    id={`${ids}-project-${index}`}
                    type="checkbox"
                    name="projects"
                    value={name}
                    checked={form.projects.includes(name)}
                    onChange={(event) => toggle("projects", name, event.target.checked)}
                    className="size-4 shrink-0 cursor-pointer accent-primary"
                    aria-invalid={errors.projects ? true : undefined}
                  />
                  <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
                </label>
              ))}
            </div>
          )}
          <Hint id={`${ids}-projects-hint`}>{t("projectsHint")}</Hint>
          <FieldError id={`${ids}-projects-error`} text={problem("projects")} testId="secret-projects-error" />
        </fieldset>

        <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={`${ids}-workers-hint`} data-testid="secret-workers">
          <legend className="mb-1.5 text-sm font-medium">{t("workers")}</legend>
          {workers.isPending ? (
            <Skeleton className="h-10 w-full sm:w-1/2" aria-hidden="true" />
          ) : workerChoices.length === 0 ? (
            <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="secret-no-workers">
              {t("noWorkers")}
            </p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {workerChoices.map((name) => (
                <Choice
                  key={name}
                  type="checkbox"
                  name="workers"
                  value={name}
                  checked={form.workers.includes(name)}
                  onChange={(on) => toggle("workers", name, on)}
                  title={<span className="font-mono">{name}</span>}
                  testId={`secret-worker-${name}`}
                />
              ))}
            </div>
          )}
          <Hint id={`${ids}-workers-hint`}>{t("workersHint")}</Hint>
        </fieldset>

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
            data-testid="secret-expires"
          />
          <Hint id={`${ids}-expires-hint`}>{t("expiresHint")}</Hint>
          <FieldError id={`${ids}-expires-error`} text={problem("expires")} testId="secret-expires-error" />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-value`}>{t("value")}</Label>
          {/* No name attribute: the value is never part of a form submission the browser could make by itself. */}
          <Input
            ref={valueRef}
            id={`${ids}-value`}
            type="password"
            defaultValue=""
            onChange={() => {
              setErrors((current) => ({ ...current, value: undefined }));
              setForgotten(false);
            }}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="h-9 font-mono"
            aria-invalid={errors.value ? true : undefined}
            aria-describedby={describedBy("value")}
            aria-required="true"
            data-1p-ignore="true"
            data-lpignore="true"
            data-bwignore="true"
            data-form-type="other"
            data-testid="secret-value"
          />
          <Hint id={`${ids}-value-hint`}>
            {adding ? t("valueHint") : t("valueReplaceHint")} {form.kind === "git" ? t("valueOneLine") : t("valueLines")}
          </Hint>
          <FieldError id={`${ids}-value-error`} text={problem("value")} testId="secret-value-error" />
        </div>

        <Box tone="warning" icon={TriangleAlert} testId="secret-warning">
          {t("warning")}
        </Box>
        {error ? (
          <div className="flex flex-col gap-2">
            <InlineError text={error.text} detail={error.detail} requestId={error.requestId} />
            {forgotten ? (
              <Box tone="info" icon={Info} testId="secret-value-forgotten">
                {t("valueForgotten")}
              </Box>
            ) : null}
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
          <Button type="submit" size="lg" disabled={noProjects} busy={pending} data-testid="secret-save">
            <LockKeyhole aria-hidden="true" />
            {pending ? t("saving") : adding ? t("add") : t("replace")}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
