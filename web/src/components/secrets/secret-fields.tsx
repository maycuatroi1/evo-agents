"use client";

import { useTranslations } from "next-intl";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import type { SecretField, SecretForm, SecretProblems } from "./model";
import { DEFAULT_GIT_USERNAME, MAX_ENV_VAR_CHARS, MAX_URL_PREFIX_CHARS, MAX_USERNAME_CHARS } from "./queries";
import { FieldError, Hint } from "./secret-form-parts";

/** What the fields of the secret form share: the form's ids, its state and how a field reports a problem. */
export type FieldProps = {
  ids: string;
  form: SecretForm;
  errors: SecretProblems;
  forge: boolean;
  describedBy: (field: SecretField, hint?: boolean) => string | undefined;
  problem: (field: SecretField) => string | null;
};

/** The fields of the secret's kind: the variable of an env secret, or the URL prefix and username of a git or forge one. */
export function KindFields({
  ids,
  form,
  errors,
  forge,
  describedBy,
  problem,
  set,
}: FieldProps & { set: (patch: Partial<SecretForm>, field?: SecretField) => void }) {
  const t = useTranslations("secrets.form");
  return form.kind === "env" ? (
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
    <div className={cn("grid gap-5", !forge && "sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]")}>
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
        <Hint id={`${ids}-urlPrefix-hint`}>{forge ? t("urlPrefixForgeHint") : t("urlPrefixHint")}</Hint>
        <FieldError id={`${ids}-urlPrefix-error`} text={problem("urlPrefix")} testId="secret-url-prefix-error" />
      </div>
      <div className={cn("flex min-w-0 flex-col gap-1.5", forge && "hidden")}>
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
  );
}

/** The projects the secret binds to, offered as checkboxes once the visitor's projects have loaded. */
export function ProjectsField({
  ids,
  form,
  errors,
  forge,
  describedBy,
  problem,
  projects,
  noProjects,
  choices,
  onToggle,
}: FieldProps & {
  projects: { isPending: boolean; isError: boolean };
  noProjects: boolean;
  choices: string[];
  onToggle: (name: string, on: boolean) => void;
}) {
  const t = useTranslations("secrets.form");
  return (
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
          {forge ? t("noAdminProjects") : t("noProjects")}
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {choices.map((name, index) => (
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
                onChange={(event) => onToggle(name, event.target.checked)}
                className="size-4 shrink-0 cursor-pointer accent-primary"
                aria-invalid={errors.projects ? true : undefined}
              />
              <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
            </label>
          ))}
        </div>
      )}
      <Hint id={`${ids}-projects-hint`}>{forge ? t("projectsForgeHint") : t("projectsHint")}</Hint>
      <FieldError id={`${ids}-projects-error`} text={problem("projects")} testId="secret-projects-error" />
    </fieldset>
  );
}
