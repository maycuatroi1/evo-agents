"use client";

import type { UseMutationResult } from "@tanstack/react-query";
import { CircleCheck, Link2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { CommandLine } from "@/components/data/command-line";
import { useVisibilityName } from "@/components/data/visibility";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import type { Project } from "@/lib/api/client";

import { LOGIN_NAME, type Role, ROLES } from "./data";
import {
  createInvite,
  INVITE_DAYS,
  INVITE_KINDS,
  type InviteCreated,
  type InviteInput,
  type InviteKind,
  inviteLink,
  validDays,
} from "./invite-data";
import { InlineError, useWriteFailure } from "./notice";
import { useAdminWrite } from "./use-admin-write";

type Props = { open: boolean; onOpenChange: (open: boolean) => void; projects: Project[] };

/**
 * Making an invite link: what it gives, how long it stays open and, if one login alone may accept it, which. The
 * answer's link is shown once, to copy; closing the dialog drops it, since the hub keeps only its hash.
 */
export function InviteDialog({ open, onOpenChange, projects }: Props) {
  const write = useAdminWrite((api, input: InviteInput) => createInvite(api, input));
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg"
        onEscapeKeyDown={guard}
        onInteractOutside={(event) => event.preventDefault()} // a click beside it must not lose a link shown once
        data-testid="invite-dialog"
      >
        <InviteSteps write={write} projects={projects} />
      </DialogContent>
    </Dialog>
  );
}

type Errors = Partial<Record<"project" | "days" | "forLogin", string>>;

function defaultLevel(project: Project | undefined): string {
  if (!project) return "";
  const level = project.default_label.level;
  return typeof level === "string" && project.levels.includes(level) ? level : (project.levels[0] ?? "");
}

function Field({ id, label, hint, error, children }: { id: string; label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

const CHOICE =
  "flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-muted/50 has-data-checked:border-brand/40 has-data-checked:bg-surface-selected";

function Choices<T extends string>({
  name,
  legend,
  values,
  value,
  onChange,
  title,
  hint,
}: {
  name: string;
  legend: string;
  values: readonly T[];
  value: T;
  onChange: (value: T) => void;
  title: (value: T) => string;
  hint: (value: T) => string;
}) {
  const ids = useId();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1.5 text-sm font-medium">{legend}</legend>
      <RadioGroup
        value={value}
        onValueChange={(next) => {
          const found = values.find((option) => option === next);
          if (found) onChange(found);
        }}
        className="gap-2"
        name={name}
      >
        {values.map((option) => (
          <Label key={option} htmlFor={`${ids}-${option}`} className={CHOICE}>
            <RadioGroupItem
              id={`${ids}-${option}`}
              value={option}
              className="mt-0.5"
              aria-labelledby={`${ids}-${option}-name`}
              aria-describedby={`${ids}-${option}-hint`}
              data-testid={`invite-${name}-${option}`}
            />
            <span className="flex flex-col gap-1">
              <span id={`${ids}-${option}-name`} className="text-sm font-medium">
                {title(option)}
              </span>
              <span id={`${ids}-${option}-hint`} className="text-xs leading-snug text-muted-foreground">
                {hint(option)}
              </span>
            </span>
          </Label>
        ))}
      </RadioGroup>
    </fieldset>
  );
}

type InviteWrite = UseMutationResult<InviteCreated, Error, InviteInput>;

function InviteSteps({ write, projects }: { write: InviteWrite; projects: Project[] }) {
  const t = useTranslations("admin.invites.form");
  const tCreated = useTranslations("admin.invites.created");
  const tKinds = useTranslations("admin.invites.kinds");
  const tRoles = useTranslations("roles");
  const tGrant = useTranslations("admin.grant");
  const tFilters = useTranslations("admin.filters");
  const levelName = useVisibilityName();
  const failure = useWriteFailure();
  const ids = useId();
  const createdHeading = useRef<HTMLHeadingElement>(null);

  const [kind, setKind] = useState<InviteKind>(projects.length === 0 ? "register" : "join");
  const [project, setProject] = useState("");
  const [role, setRole] = useState<Role>("writer");
  const [maxLevel, setMaxLevel] = useState("");
  const [days, setDays] = useState(String(INVITE_DAYS.initial));
  const [forLogin, setForLogin] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [link, setLink] = useState<string | null>(null);

  const { reset } = write;
  useEffect(() => reset(), [reset]); // a failure from an earlier opening is not this one's
  useEffect(() => {
    if (link) createdHeading.current?.focus();
  }, [link]);

  const selected = projects.find((p) => p.name === project);
  const pending = write.isPending;
  const join = kind === "join";
  const describedBy = (field: keyof Errors, hint: boolean) =>
    [hint ? `${ids}-${field}-hint` : "", errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ") || undefined;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const found: Errors = {};
    if (join && !selected) found.project = t("projectRequired");
    if (!validDays(days)) found.days = t("daysInvalid");
    if (forLogin.trim() && !LOGIN_NAME.test(forLogin.trim())) found.forLogin = tFilters("invalidLogin");
    setErrors(found);
    const first = (["project", "days", "forLogin"] as const).find((key) => found[key]);
    if (first) {
      document.getElementById(`${ids}-${first}`)?.focus();
      return;
    }
    try {
      const created = await write.mutateAsync({ kind, project, role, maxLevel, forLogin, days: Number(days) });
      setLink(inviteLink(created, window.location.origin));
    } catch {
      // shown next to the button, from the mutation's state
    }
  };

  if (link) {
    return (
      <div className="flex flex-col gap-5" data-testid="invite-created">
        <DialogHeader>
          <DialogTitle asChild>
            <h2 ref={createdHeading} tabIndex={-1} className="flex items-center gap-2 outline-none">
              <CircleCheck className="size-4 text-success" aria-hidden="true" />
              {tCreated("title")}
            </h2>
          </DialogTitle>
          <DialogDescription>{tCreated("description")}</DialogDescription>
        </DialogHeader>
        <CommandLine command={link} label={tCreated("label")} testId="invite-created-link" />
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" size="lg" data-testid="invite-created-done">
              {tCreated("done")}
            </Button>
          </DialogClose>
        </DialogFooter>
      </div>
    );
  }

  const error = write.isError ? failure(write.error, { 404: t("notFound", { project }) }) : null;
  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-busy={pending || undefined}>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Link2 className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </DialogTitle>
        <DialogDescription>{t("description")}</DialogDescription>
      </DialogHeader>

      <Choices name="kind" legend={t("kind")} values={INVITE_KINDS} value={kind} onChange={setKind} title={tKinds} hint={(value) => t(`kindHints.${value}`)} />

      {join ? (
        <>
          <Field id={`${ids}-project`} label={t("project")} error={errors.project}>
            {projects.length === 0 ? (
              <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="invite-no-projects">
                {t("noProjects")}
              </p>
            ) : (
              <NativeSelect
                id={`${ids}-project`}
                value={project}
                onChange={(event) => {
                  setProject(event.target.value);
                  setMaxLevel(defaultLevel(projects.find((p) => p.name === event.target.value)));
                  setErrors((e) => ({ ...e, project: undefined }));
                }}
                className="w-full md:[&_select]:h-9"
                aria-invalid={errors.project ? true : undefined}
                aria-describedby={describedBy("project", false)}
                aria-required="true"
                data-testid="invite-project"
              >
                <NativeSelectOption value="" disabled>
                  {t("projectPlaceholder")}
                </NativeSelectOption>
                {projects.map((p) => (
                  <NativeSelectOption key={p.name} value={p.name}>
                    {p.name}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Choices name="role" legend={t("role")} values={ROLES} value={role} onChange={setRole} title={tRoles} hint={(value) => tGrant(`roleHints.${value}`)} />
          <Field id={`${ids}-maxLevel`} label={t("visibility")} hint={t("visibilityHint")}>
            <NativeSelect
              id={`${ids}-maxLevel`}
              value={maxLevel}
              onChange={(event) => setMaxLevel(event.target.value)}
              disabled={!selected}
              className="w-full md:[&_select]:h-9"
              aria-describedby={`${ids}-maxLevel-hint`}
              data-testid="invite-max-level"
            >
              {(selected?.levels ?? []).map((level) => (
                <NativeSelectOption key={level} value={level}>
                  {levelName(level)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
        </>
      ) : null}

      <Field id={`${ids}-days`} label={t("days")} hint={t("daysHint")} error={errors.days}>
        <Input
          id={`${ids}-days`}
          type="number"
          inputMode="numeric"
          min={INVITE_DAYS.min}
          max={INVITE_DAYS.max}
          step={1}
          value={days}
          onChange={(event) => {
            setDays(event.target.value);
            setErrors((e) => ({ ...e, days: undefined }));
          }}
          className="h-9 w-28 tabular-nums"
          aria-invalid={errors.days ? true : undefined}
          aria-describedby={describedBy("days", true)}
          data-testid="invite-days"
        />
      </Field>

      <Field id={`${ids}-forLogin`} label={t("forLogin")} hint={t("forLoginHint")} error={errors.forLogin}>
        <Input
          id={`${ids}-forLogin`}
          value={forLogin}
          onChange={(event) => {
            setForLogin(event.target.value);
            setErrors((e) => ({ ...e, forLogin: undefined }));
          }}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={100}
          className="h-9 font-mono"
          aria-invalid={errors.forLogin ? true : undefined}
          aria-describedby={describedBy("forLogin", true)}
          data-testid="invite-for-login"
        />
      </Field>

      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg" disabled={pending}>
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={pending} disabled={join && projects.length === 0} data-testid="invite-create">
          {pending ? t("creating") : t("create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
