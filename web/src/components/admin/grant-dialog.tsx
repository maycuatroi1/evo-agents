"use client";

import type { UseMutationResult } from "@tanstack/react-query";
import { ArrowLeft, Info, ShieldPlus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { RoleBadge } from "@/components/shell/role-badge";
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
import type { components } from "@/lib/api/schema";

import { type AdminUser, type GrantInput, LOGIN_NAME, putGrant, type Role, ROLES } from "./data";
import { InlineError, type Notice, useWriteFailure } from "./notice";
import { useAdminWrite } from "./use-admin-write";

/** What the dialog opens with: empty, a user (from their page), or one of their grants (to change it). */
export type GrantPreset = { login?: string; project?: string; role?: Role; maxLevel?: string; lockLogin?: boolean };

type Grant = components["schemas"]["Grant"];
type GrantWrite = UseMutationResult<Grant, Error, GrantInput>;

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  users: AdminUser[];
  projects: Project[];
  preset: GrantPreset;
  onDone: (notice: Notice) => void;
};

/**
 * Granting a role on a project, in two steps: the form, then a summary to confirm. Nothing is written before the
 * confirmation. While the write runs the dialog cannot be dismissed, and a refusal is shown next to the button that
 * caused it. The form starts afresh each time the dialog opens (its content mounts on open).
 */
export function GrantDialog({ open, onOpenChange, ...rest }: Props) {
  const write = useAdminWrite((api, input: GrantInput) => putGrant(api, input));
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
        onInteractOutside={guard}
        data-testid="grant-dialog"
      >
        <GrantSteps write={write} onClose={() => onOpenChange(false)} {...rest} />
      </DialogContent>
    </Dialog>
  );
}

type Errors = Partial<Record<"login" | "project" | "maxLevel", string>>;

function defaultLevel(project: Project | undefined): string {
  if (!project) return "";
  const level = project.default_label.level;
  return typeof level === "string" && project.levels.includes(level) ? level : (project.levels[0] ?? "");
}

function isRole(value: string): value is Role {
  return (ROLES as readonly string[]).includes(value);
}

function GrantSteps({
  write,
  onClose,
  users,
  projects,
  preset,
  onDone,
}: Omit<Props, "open" | "onOpenChange"> & { write: GrantWrite; onClose: () => void }) {
  const t = useTranslations("admin.grant");
  const tRoles = useTranslations("roles");
  const tFilters = useTranslations("admin.filters");
  const failure = useWriteFailure();
  const ids = useId();
  const confirmHeading = useRef<HTMLHeadingElement>(null);

  const initialProject = projects.find((p) => p.name === preset.project);
  const [step, setStep] = useState<"form" | "confirm">("form");
  const [login, setLogin] = useState(preset.login ?? "");
  const [project, setProject] = useState(initialProject?.name ?? "");
  const [role, setRole] = useState<Role>(preset.role ?? "reader");
  const [maxLevel, setMaxLevel] = useState(
    preset.maxLevel && initialProject?.levels.includes(preset.maxLevel) ? preset.maxLevel : defaultLevel(initialProject),
  );
  const [errors, setErrors] = useState<Errors>({});

  const { reset } = write;
  useEffect(() => reset(), [reset]); // a failure from an earlier opening is not this one's
  useEffect(() => {
    if (step === "confirm") confirmHeading.current?.focus();
  }, [step]);

  const selected = projects.find((p) => p.name === project);
  const name = login.trim();
  const grantee = users.find((u) => u.login.toLowerCase() === name.toLowerCase());
  const existing = grantee?.grants.find((g) => g.project === project);
  const unchanged = existing !== undefined && existing.role === role && existing.max_level === maxLevel;
  const pending = write.isPending;
  const roleName = (value: string) => tRoles(isRole(value) ? value : "none");

  const changeProject = (next: string) => {
    setProject(next);
    const ladder = projects.find((p) => p.name === next);
    const current = grantee?.grants.find((g) => g.project === next);
    if (current && isRole(current.role)) setRole(current.role);
    setMaxLevel(current && ladder?.levels.includes(current.max_level) ? current.max_level : defaultLevel(ladder));
    setErrors((e) => ({ ...e, project: undefined, maxLevel: undefined }));
  };

  const submitForm = (event: FormEvent) => {
    event.preventDefault();
    const found: Errors = {};
    if (!name) found.login = t("loginRequired");
    else if (!LOGIN_NAME.test(name)) found.login = tFilters("invalidLogin");
    if (!selected) found.project = t("projectRequired");
    else if (!selected.levels.includes(maxLevel)) found.maxLevel = t("maxLevelRequired");
    setErrors(found);
    const first = (["login", "project", "maxLevel"] as const).find((key) => found[key]);
    if (first) {
      document.getElementById(`${ids}-${first}`)?.focus();
      return;
    }
    write.reset();
    setStep("confirm");
  };

  const confirm = async () => {
    if (pending) return;
    let grant: Grant;
    try {
      grant = await write.mutateAsync({ login: name, project, role, maxLevel }); // resolves once the lists are fresh
    } catch {
      return; // shown next to the button, from the mutation's state
    }
    onDone({
      tone: "success",
      text: t("success", { role: roleName(grant.role), level: grant.max_level, login: grant.login, project: grant.project }),
    });
    onClose();
  };

  const error = write.isError ? failure(write.error, { 404: t("notFound", { project }) }) : null;
  const describedBy = (field: keyof Errors, hint?: boolean) =>
    [hint ? `${ids}-${field}-hint` : "", errors[field] ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ") || undefined;

  if (step === "confirm") {
    return (
      <div className="flex flex-col gap-5" aria-busy={pending || undefined}>
        <DialogHeader>
          <DialogTitle asChild>
            <h2 ref={confirmHeading} tabIndex={-1} className="outline-none">
              {existing ? t("confirmChangeTitle") : t("confirmTitle")}
            </h2>
          </DialogTitle>
          <DialogDescription>{t("confirmDescription")}</DialogDescription>
        </DialogHeader>
        <dl
          className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2.5 rounded-md border bg-muted/40 px-3 py-3 text-sm"
          data-testid="grant-summary"
        >
          <dt className="text-muted-foreground">{t("summary.login")}</dt>
          <dd className="min-w-0 font-mono font-medium break-all">{name}</dd>
          <dt className="text-muted-foreground">{t("summary.project")}</dt>
          <dd className="min-w-0 font-mono font-medium break-all">{project}</dd>
          <dt className="text-muted-foreground">{t("summary.role")}</dt>
          <dd>
            <RoleBadge role={role} />
          </dd>
          <dt className="text-muted-foreground">{t("summary.maxLevel")}</dt>
          <dd className="font-mono font-medium">{maxLevel}</dd>
          {existing ? (
            <>
              <dt className="text-muted-foreground">{t("summary.previous")}</dt>
              <dd className="text-muted-foreground">{t("previous", { role: roleName(existing.role), level: existing.max_level })}</dd>
            </>
          ) : null}
        </dl>
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            size="lg"
            onClick={() => {
              if (!pending) setStep("form");
            }}
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
          >
            <ArrowLeft aria-hidden="true" />
            {t("back")}
          </Button>
          <Button
            type="button"
            size="lg"
            onClick={() => void confirm()}
            busy={pending}
            data-testid="grant-confirm"
          >
            {pending ? t("saving") : t("confirm")}
          </Button>
        </DialogFooter>
      </div>
    );
  }

  return (
    <form onSubmit={submitForm} noValidate className="flex flex-col gap-5">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <ShieldPlus className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </DialogTitle>
        <DialogDescription>{t("description")}</DialogDescription>
      </DialogHeader>

      {preset.lockLogin ? (
        <p className="flex flex-wrap items-baseline gap-x-2 text-sm">
          <span className="text-muted-foreground">{t("summary.login")}:</span>
          <span className="font-mono font-medium break-all">{name}</span>
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-login`}>{t("login")}</Label>
          <Input
            id={`${ids}-login`}
            name="login"
            value={login}
            onChange={(event) => {
              setLogin(event.target.value);
              setErrors((e) => ({ ...e, login: undefined }));
            }}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            list={`${ids}-logins`}
            maxLength={100}
            className="h-9 font-mono"
            aria-invalid={errors.login ? true : undefined}
            aria-describedby={describedBy("login", true)}
            aria-required="true"
            data-testid="grant-login"
          />
          <datalist id={`${ids}-logins`}>
            {users.map((u) => (
              <option key={u.login} value={u.login} />
            ))}
          </datalist>
          <p id={`${ids}-login-hint`} className="text-xs text-muted-foreground">
            {t("loginHint")}
          </p>
          {errors.login ? (
            <p id={`${ids}-login-error`} className="text-xs font-medium text-danger">
              {errors.login}
            </p>
          ) : null}
        </div>
      )}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-project`}>{t("project")}</Label>
        {projects.length === 0 ? (
          <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="grant-no-projects">
            {t("noProjects")}
          </p>
        ) : (
          <NativeSelect
            id={`${ids}-project`}
            name="project"
            value={project}
            onChange={(event) => changeProject(event.target.value)}
            className="w-full [&_select]:h-9"
            aria-invalid={errors.project ? true : undefined}
            aria-describedby={describedBy("project")}
            aria-required="true"
            data-testid="grant-project"
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
        {errors.project ? (
          <p id={`${ids}-project-error`} className="text-xs font-medium text-danger">
            {errors.project}
          </p>
        ) : null}
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1.5 text-sm font-medium">{t("role")}</legend>
        <RadioGroup
          value={role}
          onValueChange={(value) => {
            if (isRole(value)) setRole(value);
          }}
          className="gap-2"
          name="role"
          data-testid="grant-role"
        >
          {ROLES.map((value) => (
            <Label
              key={value}
              htmlFor={`${ids}-role-${value}`}
              className="flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 font-normal transition-colors hover:bg-muted/50 has-data-checked:border-brand/40 has-data-checked:bg-surface-selected"
            >
              <RadioGroupItem
                id={`${ids}-role-${value}`}
                value={value}
                className="mt-0.5"
                aria-labelledby={`${ids}-role-${value}-name`}
                aria-describedby={`${ids}-role-${value}-hint`}
              />
              <span className="flex flex-col gap-1">
                <span id={`${ids}-role-${value}-name`} className="text-sm font-medium">
                  {tRoles(value)}
                </span>
                <span id={`${ids}-role-${value}-hint`} className="text-xs leading-snug text-muted-foreground">
                  {t(`roleHints.${value}`)}
                </span>
              </span>
            </Label>
          ))}
        </RadioGroup>
      </fieldset>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-maxLevel`}>{t("maxLevel")}</Label>
        <NativeSelect
          id={`${ids}-maxLevel`}
          name="max_level"
          value={maxLevel}
          onChange={(event) => {
            setMaxLevel(event.target.value);
            setErrors((e) => ({ ...e, maxLevel: undefined }));
          }}
          disabled={!selected}
          className="w-full [&_select]:h-9 [&_select]:font-mono"
          aria-invalid={errors.maxLevel ? true : undefined}
          aria-describedby={describedBy("maxLevel", true)}
          data-testid="grant-max-level"
        >
          {(selected?.levels ?? []).map((level) => (
            <NativeSelectOption key={level} value={level}>
              {level}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        <p id={`${ids}-maxLevel-hint`} className="text-xs text-muted-foreground">
          {t("maxLevelHint")}
        </p>
        {errors.maxLevel ? (
          <p id={`${ids}-maxLevel-error`} className="text-xs font-medium text-danger">
            {errors.maxLevel}
          </p>
        ) : null}
      </div>

      {existing ? (
        <p className="flex items-start gap-2 rounded-md border bg-accent px-3 py-2.5 text-sm text-accent-foreground" data-testid="grant-existing">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {unchanged
            ? t("unchanged", { login: grantee?.login ?? name })
            : t("existing", { login: grantee?.login ?? name, role: roleName(existing.role), level: existing.max_level })}
        </p>
      ) : null}

      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline" size="lg">
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" disabled={projects.length === 0 || unchanged} data-testid="grant-continue">
          {t("continue")}
        </Button>
      </DialogFooter>
    </form>
  );
}
