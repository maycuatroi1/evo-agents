"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDashed, CircleMinus, CircleX, LoaderCircle, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { browserApi } from "@/lib/api/browser";
import { kgHref } from "@/lib/kg/routes";
import { type KgPipeline, type KgPipelinePhase, kgPipelineQuery } from "@/lib/kg/upload-queries";

const LINK = "rounded-xs text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:py-3";

const ICONS: Record<KgPipelinePhase["state"], LucideIcon> = {
  pending: CircleDashed,
  running: LoaderCircle,
  done: CircleCheck,
  skipped: CircleMinus,
  failed: CircleX,
};

const TONES: Record<KgPipelinePhase["state"], string> = {
  pending: "text-muted-foreground",
  running: "text-foreground",
  done: "text-foreground",
  skipped: "text-muted-foreground",
  failed: "text-danger",
};

/**
 * The flow after an upload, as the hub runs it by itself: the archive sent and read, the graph built, the starter
 * questions, the Curator's setup and the entities step, each with its state and what it gave. Running, it names the
 * phase; ready, it links the graph; failed, it names the phase and the hub's reason.
 */
export function PipelineView({ project, pipeline }: { project: string; pipeline: KgPipeline }) {
  const t = useTranslations("kgUpload.pipeline");
  const phase = pipeline.phase ? t(`phases.${pipeline.phase as KgPipelinePhase["name"]}`) : "";
  return (
    <section
      className="flex flex-col gap-2 rounded-md border bg-muted/30 p-3"
      aria-label={t("title")}
      data-testid="kg-upload-pipeline"
      data-state={pipeline.state}
      data-phase={pipeline.phase ?? ""}
    >
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm font-medium" aria-live="polite" data-testid="kg-upload-pipeline-headline">
        {pipeline.state === "running" ? <span>{t("running", { phase })}</span> : null}
        {pipeline.state === "ready" ? (
          <>
            <span>{t("ready")}</span>
            <Link href={kgHref(project)} className={LINK} data-testid="kg-upload-pipeline-open">
              {t("open")}
            </Link>
          </>
        ) : null}
        {pipeline.state === "failed" ? <span className="text-danger">{t("failed", { phase })}</span> : null}
        {pipeline.state === "replaced" ? <span className="text-muted-foreground">{t("replaced")}</span> : null}
      </p>
      {pipeline.state === "failed" && pipeline.error ? (
        <p className="text-sm text-danger [overflow-wrap:anywhere]" data-testid="kg-upload-pipeline-error">
          <span className="font-medium">{t("reason")}: </span>
          {pipeline.error}
        </p>
      ) : null}
      <ol className="flex flex-col gap-1">
        {pipeline.phases.map((step) => {
          const Icon = ICONS[step.state];
          return (
            <li
              key={step.name}
              className={`flex flex-wrap items-baseline gap-x-2 text-xs ${TONES[step.state]}`}
              data-testid="kg-upload-pipeline-phase"
              data-phase={step.name}
              data-state={step.state}
            >
              <Icon className={`size-3.5 shrink-0 self-center ${step.state === "running" ? "motion-safe:animate-spin" : ""}`} aria-hidden="true" />
              <span className="font-medium">{t(`phases.${step.name}`)}</span>
              <span>{t(`states.${step.state}`)}</span>
              {step.detail ? <span className="text-muted-foreground [overflow-wrap:anywhere]">{step.detail}</span> : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The flow after upload `upload`, read from the hub and read again every few seconds until it ends. */
export function UploadPipeline({ project, upload }: { project: string; upload: number }) {
  const t = useTranslations("kgUpload.pipeline");
  const pipeline = useQuery(kgPipelineQuery(browserApi, project, upload));
  if (pipeline.data) return <PipelineView project={project} pipeline={pipeline.data} />;
  if (pipeline.error) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="kg-upload-pipeline-unread">
        {t("loadFailed", { message: pipeline.error.message })}
      </p>
    );
  }
  return null;
}
