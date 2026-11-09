"use client";

import { useQueryClient } from "@tanstack/react-query";
import { FolderPlus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useId, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { uploadHref } from "@/lib/kg/routes";
import { NEW_PROJECT_LEVEL, NEW_PROJECT_LEVELS } from "@/lib/kg/upload-labels";
import { PROJECT_NAME, queryKeys } from "@/lib/queries";

import { ArchiveDrop } from "./archive-drop";
import { FieldError, FieldHint, UploadFields } from "./upload-fields";
import { UploadPipeline } from "./upload-pipeline";
import { UploadProgress } from "./upload-progress";
import { isBusy, type UploadPhase, useUpload } from "./use-upload";

const LINK = "font-medium text-brand underline underline-offset-4 hover:text-brand-hover";

/**
 * What a refused start means here, in this page's words: no right to create a project (403), or a name taken (409),
 * where the project's own Upload data page is the way on; otherwise the hub's own words (UploadProgress).
 */
function Refusal({ state, project }: { state: UploadPhase; project: string }) {
  const t = useTranslations("kgUpload.newProject");
  if (state.phase !== "failed") return null;
  if (state.error.status === 403) {
    return (
      <p role="alert" className="text-sm font-medium text-danger" data-testid="kg-new-project-forbidden">
        {t("forbidden")}
      </p>
    );
  }
  if (state.error.status === 409 && /exists already/.test(state.error.message)) {
    return (
      <p role="alert" className="text-sm font-medium text-danger" data-testid="kg-new-project-taken">
        {t("taken", { name: project })}{" "}
        <Link href={uploadHref(project)} className={LINK}>
          {t("open")}
        </Link>
      </p>
    );
  }
  return null;
}

/**
 * /projects/new, New project from data: a name, an archive, its source and label, then one call that has the hub
 * create the project with the default ladder and a hub sink clearing that label, and start the upload into it
 * (POST /v1/kg/{project}/uploads with `new_project`). The parts follow as on Upload data; once joined, the project's
 * Upload data page shows the report, the build and the kg_init run that the first build queues. Creating takes a hub
 * admin or an unused register right, which only the hub knows: the page offers it to everyone and says why it was
 * refused.
 */
export function NewProjectPage() {
  const t = useTranslations("kgUpload");
  const ids = useId();
  // Once the hub created the project (it took the upload), a retry resumes the upload into it.
  const [created, setCreated] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const onStarted = useCallback(
    (_: unknown, options: { project: string }) => {
      setCreated(options.project);
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects }); // the switcher lists it now
    },
    [queryClient],
  );
  const { state, start, cancel } = useUpload({ onStarted });
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [label, setLabel] = useState<string>(NEW_PROJECT_LEVEL);
  const busy = isBusy(state);
  const done = state.phase === "done";
  const project = name.trim();
  const nameValid = PROJECT_NAME.test(project);
  const nameError = touched && !nameValid ? t("newProject.nameInvalid") : null;
  const go = () => {
    setTouched(true);
    if (!file || !nameValid) return;
    void start(file, { project, source, label, newProject: created !== project });
  };
  const known = state.phase === "failed" && (state.error.status === 403 || (state.error.status === 409 && /exists already/.test(state.error.message)));
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title={t("newProject.title")} testId="kg-new-project-header" />
      <form
        className="flex flex-col gap-4 rounded-md border bg-card p-4 shadow-raised"
        noValidate
        aria-busy={busy || undefined}
        onSubmit={(event) => {
          event.preventDefault();
          go();
        }}
        data-testid="kg-new-project-form"
      >
        <div className="flex max-w-md flex-col gap-1.5">
          <Label htmlFor={`${ids}-name`}>{t("newProject.name")}</Label>
          <Input
            id={`${ids}-name`}
            value={name}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => setTouched(true)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={100}
            className="font-mono"
            aria-invalid={nameError ? true : undefined}
            aria-describedby={`${ids}-name-hint${nameError ? ` ${ids}-name-error` : ""}`}
            aria-required="true"
            disabled={busy || done || created !== null}
            data-testid="kg-new-project-name"
          />
          <FieldHint id={`${ids}-name-hint`}>{t("newProject.nameHint")}</FieldHint>
          <FieldError id={`${ids}-name-error`} text={nameError} testId="kg-new-project-name-error" />
        </div>
        <ArchiveDrop file={file} onFile={setFile} disabled={busy || done} />
        <UploadFields
          file={file}
          source={source}
          onSource={setSource}
          levels={NEW_PROJECT_LEVELS}
          label={label}
          onLabel={setLabel}
          labelHint={t("newProject.labelHint")}
          disabled={busy || done}
        />
        {done ? null : (
          <div>
            <Button type="submit" disabled={!file || busy} data-testid="kg-new-project-create">
              <FolderPlus aria-hidden="true" />
              {t("newProject.create")}
            </Button>
          </div>
        )}
        <Refusal state={state} project={project} />
        {known ? null : (
          <UploadProgress
            state={state}
            onCancel={cancel}
            onRetry={go}
            doneText={
              done ? (
                <>
                  {t("newProject.done", { name: project })}{" "}
                  <Link href={uploadHref(project)} className={LINK} data-testid="kg-new-project-open">
                    {t("newProject.open")}
                  </Link>
                </>
              ) : null
            }
          />
        )}
        {state.phase === "done" ? <UploadPipeline project={project} upload={state.upload.id} /> : null}
      </form>
    </div>
  );
}
