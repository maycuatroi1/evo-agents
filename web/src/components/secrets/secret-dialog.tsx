"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, LockKeyhole, RefreshCw, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { InlineError } from "@/components/admin/notice";
import type { Notice } from "@/components/feedback/toast";
import { Choice, Section } from "@/components/runs/dispatch-fields";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { poolsQuery, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { projectsQuery, whoamiQuery } from "@/lib/queries";

import { BindingPicker } from "./binding-picker";
import { type PutSecret, usePutSecret, useSecretFailure } from "./hooks";
import {
  emptySecretForm,
  formOf,
  SECRET_FIELDS,
  secretBody,
  type SecretField,
  type SecretForm,
  oneLine,
  secretProblems,
  type SecretProblems,
  utcDay,
} from "./model";
import { MAX_SECRET_BYTES, MAX_SECRET_NAME_CHARS, type Secret } from "./queries";
import { KindFields, ProjectsField } from "./secret-fields";
import { Box, FieldError, Hint } from "./secret-form-parts";

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
  const pools = useQuery(poolsQuery(browserApi));
  const [form, setForm] = useState<SecretForm>(() => (secret ? formOf(secret) : emptySecretForm()));
  const [errors, setErrors] = useState<SecretProblems>({});
  const [forgotten, setForgotten] = useState(false);
  const valueRef = useRef<HTMLInputElement>(null);
  const [minDay] = useState(() => tomorrow(Date.now()));
  const pending = write.pending;

  // A project or worker the secret names stays offered on a replace, even when the visitor no longer writes to that
  // project or the worker is gone: the hub then says why it refuses.
  // A forge secret merges into the project's repos: it binds only to projects the visitor administers.
  const forge = form.kind === "forge";
  const projectChoices = useMemo(() => {
    const roles = forge ? ["admin"] : ["writer", "admin"];
    const writable = (projects.data ?? []).filter((project) => roles.includes(project.role ?? "")).map((project) => project.name);
    return [...new Set([...writable, ...(secret?.projects ?? [])])].sort();
  }, [projects.data, secret, forge]);
  const workerChoices = useMemo(() => {
    const own = (workers.data ?? []).filter((worker) => worker.owner === me?.login && worker.status !== "revoked").map((worker) => worker.name);
    return [...new Set([...own, ...(secret?.workers ?? [])])].sort((a, b) => a.localeCompare(b));
  }, [workers.data, me?.login, secret]);
  const poolChoices = useMemo(() => {
    const own = (pools.data ?? []).filter((pool) => pool.owner === me?.login).map((pool) => pool.name);
    return [...new Set([...own, ...(secret?.pools ?? [])])].sort((a, b) => a.localeCompare(b));
  }, [pools.data, me?.login, secret]);
  const noProjects = !projects.isPending && !projects.isError && projectChoices.length === 0;

  const set = (patch: Partial<SecretForm>, field?: SecretField) => {
    setForm((current) => ({ ...current, ...patch }));
    if (field) setErrors((current) => ({ ...current, [field]: undefined }));
  };
  const toggle = (list: "projects" | "workers" | "pools", name: string, on: boolean) =>
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
          <div className="grid gap-2 sm:grid-cols-3">
            {kindChoice("env")}
            {kindChoice("git")}
            {kindChoice("forge")}
          </div>
        </Section>

        <KindFields ids={ids} form={form} errors={errors} forge={forge} describedBy={describedBy} problem={problem} set={set} />

        <ProjectsField
          ids={ids} form={form} errors={errors} forge={forge} describedBy={describedBy} problem={problem}
          projects={projects}
          noProjects={noProjects}
          choices={projectChoices}
          onToggle={(name, on) => toggle("projects", name, on)}
        />

        {(["workers", "pools"] as const).map((list) => {
          const [kind, locked] = list === "workers" ? (["worker", form.pools.length > 0] as const) : (["pool", form.workers.length > 0] as const);
          return (
            <BindingPicker
              key={list}
              id={`${ids}-${list}`}
              kind={kind}
              legend={t(list)}
              hint={t(locked ? `${list}Locked` : `${list}Hint`)}
              empty={t(list === "workers" ? "noWorkers" : "noPools")}
              loading={(list === "workers" ? workers : pools).isPending}
              hidden={forge}
              choices={list === "workers" ? workerChoices : poolChoices}
              chosen={form[list]}
              locked={locked}
              onToggle={(name, on) => toggle(list, name, on)}
              choice={Choice}
            />
          );
        })}

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
          <Hint id={`${ids}-expires-hint`}>{forge ? t("expiresForgeHint") : t("expiresHint")}</Hint>
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
            {adding ? t("valueHint") : t("valueReplaceHint")} {forge ? t("valueForge") : oneLine(form.kind) ? t("valueOneLine") : t("valueLines")}
          </Hint>
          <FieldError id={`${ids}-value-error`} text={problem("value")} testId="secret-value-error" />
        </div>

        <Box tone="warning" icon={TriangleAlert} testId="secret-warning">
          {forge ? t("warningForge") : t("warning")}
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
