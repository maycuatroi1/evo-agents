"use client";

import { useQuery } from "@tanstack/react-query";
import { FileArchive, History, Lock, Upload as UploadIcon, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { useByteSize } from "@/components/data/byte-size";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { uploadLevels } from "@/lib/kg/upload-labels";
import { initRunFor, kgInitRunsQuery, kgPipelineQuery, kgUploadsQuery, uploadMoving } from "@/lib/kg/upload-queries";
import {
  forgetUpload,
  type PendingUpload,
  pendingFor,
  pendingSnapshot,
  pendingUploads,
  subscribePending,
} from "@/lib/kg/upload-resume";
import { projectQuery } from "@/lib/queries";

import { KgHeader } from "../kg-header";
import { ArchiveDrop } from "./archive-drop";
import { UploadFields } from "./upload-fields";
import { UploadProgress } from "./upload-progress";
import { UploadReport } from "./upload-report";
import { isBusy, useUpload } from "./use-upload";

export type KgUploadErrors = { project: ApiErrorInfo | null; uploads: ApiErrorInfo | null };

/**
 * The uploads this browser left unfinished in `project` (a reload, a closed tab, a lost network) and still uploading
 * on the hub (`live`, once read): choosing the same file again resumes each. Records of uploads that ended elsewhere
 * (finished from the CLI, failed after a day) are dropped. localStorage is read after hydration, as the server has none.
 */
function usePending(project: string, live: number[] | null): PendingUpload[] {
  const raw = useSyncExternalStore(subscribePending, pendingSnapshot, () => "");
  // `raw` is what changes: the records are read again when it does.
  const records = useMemo(() => (raw ? pendingUploads(project) : []), [raw, project]);
  const key = live?.join(",") ?? null;
  useEffect(() => {
    if (key === null) return;
    const open = new Set(key ? key.split(",").map(Number) : []);
    for (const record of pendingUploads(project)) if (!open.has(record.id)) forgetUpload(project, record.id);
  }, [project, key]);
  return live === null ? [] : records.filter((record) => live.includes(record.id));
}

function PendingNotice({ records, onForget }: { records: PendingUpload[]; onForget: (id: number) => void }) {
  const t = useTranslations("kgUpload.pending");
  const size = useByteSize();
  if (records.length === 0) return null;
  return (
    <Banner icon={History} tone="attention" title={t("title", { count: records.length })} testId="kg-upload-pending">
      <p>{t("body")}</p>
      <ul className="mt-1.5 flex flex-col gap-1">
        {records.map((record) => (
          <li key={record.id} className="flex items-center gap-2" data-testid="kg-upload-pending-item" data-upload={record.id}>
            <FileArchive className="size-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0 truncate font-mono text-[13px]">{record.name}</span>
            <span className="text-xs tabular-nums">{size(record.size)}</span>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={t("forget", { name: record.name })} onClick={() => onForget(record.id)}>
              <X aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
    </Banner>
  );
}

/** The form: the archive, its source and label, Upload, then where the upload stands. */
function UploadCard({ project, info, upload }: { project: string; info: Project; upload: ReturnType<typeof useUpload> }) {
  const t = useTranslations("kgUpload");
  const levels = uploadLevels(info);
  const { state, start, cancel, reset } = upload;
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [label, setLabel] = useState<string | null>(null);
  if (levels.kind !== "levels") {
    return (
      <StatePanel
        icon={Lock}
        title={t("title")}
        description={levels.kind === "not-writer" ? t("levels.notWriter") : t("levels.noHubSink")}
        testId={`kg-upload-${levels.kind}`}
      />
    );
  }
  const busy = isBusy(state);
  const resumes = file !== null && !busy && state.phase !== "done" && pendingFor(project, file) !== null;
  const picked = label ?? levels.preset;
  const go = () => {
    if (file) void start(file, { project, source, label: picked });
  };
  const choose = (next: File) => {
    setFile(next);
    const known = pendingFor(project, next);
    if (known) {
      setSource(known.source);
      if (known.label && levels.levels.includes(known.label)) setLabel(known.label);
    }
    if (!busy) reset();
  };
  const another = () => {
    setFile(null);
    setSource("");
    reset();
  };
  return (
    <section className="flex flex-col gap-4 rounded-md border bg-card p-4 shadow-raised" aria-labelledby="kg-upload-form-title" data-testid="kg-upload-form">
      <h2 id="kg-upload-form-title" className="sr-only">
        {t("title")}
      </h2>
      <ArchiveDrop file={file} onFile={choose} disabled={busy} />
      <UploadFields
        file={file}
        source={source}
        onSource={setSource}
        levels={levels.levels}
        label={picked}
        onLabel={setLabel}
        labelHint={t("form.labelHint")}
        disabled={busy}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={go} disabled={!file || busy} data-testid="kg-upload-start">
          <UploadIcon aria-hidden="true" />
          {resumes ? t("form.resume") : t("form.start")}
        </Button>
        {resumes ? <span className="text-xs text-muted-foreground">{t("form.resumeHint")}</span> : null}
      </div>
      <UploadProgress state={state} onCancel={cancel} onRetry={go} onAnother={another} />
    </section>
  );
}

function UploadList({ project, error }: { project: string; error: ApiErrorInfo | null }) {
  const t = useTranslations("kgUpload.reports");
  const uploads = useHubQuery(kgUploadsQuery(browserApi, project), error);
  const list = uploads.status === "success" ? uploads.data.uploads : [];
  const anyBuilt = list.some((upload) => upload.build_id != null);
  const init = useQuery({ ...kgInitRunsQuery(browserApi, project, list.some(uploadMoving)), enabled: anyBuilt });
  // The newest upload shows its pipeline: its links name the pipeline's own kg_init run (the same read, cached once).
  const newest = list[0];
  const flow = useQuery({ ...kgPipelineQuery(browserApi, project, newest?.id ?? 0), enabled: newest != null });
  return (
    <section className="flex flex-col gap-3" aria-labelledby="kg-uploads-title" data-testid="kg-uploads">
      <h2 id="kg-uploads-title" className="text-base font-semibold">
        {t("title")}
      </h2>
      <QueryView state={uploads} loading={<ListSkeleton rows={3} />}>
        {({ uploads: rows }) =>
          rows.length === 0 ? (
            <EmptyState icon={FileArchive} title={t("emptyTitle")} testId="kg-uploads-empty" />
          ) : (
            <ul className="rounded-md border bg-card shadow-raised">
              {rows.map((upload, index) => (
                <UploadReport
                  key={upload.id}
                  project={project}
                  upload={upload}
                  initRun={initRunFor(upload, init.data?.runs, index === 0 ? flow.data?.init_id : undefined)}
                  pipeline={index === 0}
                />
              ))}
            </ul>
          )
        }
      </QueryView>
    </section>
  );
}

/**
 * /p/{project}/kg/upload, Upload data: an archive dropped or chosen is checked, sent in parts (each part's progress
 * shown) and joined; the hub then reads it as the source upload.{name} and builds the graph. A reload mid-way keeps
 * what was sent: the page lists the unfinished uploads, and the same file chosen again sends only what is missing.
 * Under the form, each upload's report: files read, files skipped and kept without text by reason, its build and the
 * kg_init run after it; the newest also shows the flow the hub runs after it, until the graph is ready.
 */
export function KgUploadPage({ project, errors }: { project: string; errors: KgUploadErrors }) {
  const info = useHubQuery(projectQuery(browserApi, project), errors.project);
  const uploads = useQuery(kgUploadsQuery(browserApi, project));
  const live = uploads.data ? uploads.data.uploads.filter((upload) => upload.state === "uploading").map((upload) => upload.id) : null;
  const upload = useUpload();
  const { state } = upload;
  const records = usePending(project, live);
  // The upload this page is sending is not one left unfinished.
  const inFlight = state.phase === "sending" || state.phase === "completing" ? state.started.id : null;
  const pending = records.filter((record) => record.id !== inFlight);
  return (
    <div className="flex flex-col gap-6">
      <KgHeader project={project} current="upload" />
      <PendingNotice records={pending} onForget={(id) => forgetUpload(project, id)} />
      <QueryView state={info} loading={<Skeleton className="h-64 w-full rounded-md" />}>
        {(data) => <UploadCard project={project} info={data} upload={upload} />}
      </QueryView>
      <UploadList project={project} error={errors.uploads} />
    </div>
  );
}

export function KgUploadSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-16 w-full rounded-md" />
      <Skeleton className="h-64 w-full rounded-md" />
      <ListSkeleton rows={3} />
    </div>
  );
}
