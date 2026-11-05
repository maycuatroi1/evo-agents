"use client";

import { CircleX } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Ago } from "@/components/data/ago";
import { useByteSize } from "@/components/data/byte-size";
import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill } from "@/components/status/status-badge";
import type { KgUpload } from "@/lib/kg/upload";
import type { KgInitRun } from "@/lib/kg/upload-queries";
import { buildsHref, initRunHref } from "@/lib/kg/routes";

import { UploadPipeline } from "./upload-pipeline";

const LINK = "rounded-xs text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:py-3";

/**
 * Only what went wrong gets a pill: an upload or its build that failed. One still moving says so in plain words beside
 * its name; one read and built says so by its report alone.
 */
const FAILED: StatusLook = { tone: "danger", icon: CircleX };
const MOVING = new Set(["uploading", "extracting"]);
const BUILDING = new Set(["queued", "running"]);

const REASONS = new Set([
  "duplicate",
  "too_many_entries",
  "total_size",
  "depth",
  "corrupt",
  "symlink",
  "link",
  "device",
  "encrypted",
  "ratio",
  "unsafe_path",
  "binary",
  "unreadable",
  "no_text_layer",
  "timeout",
  "too_large",
]);

/** A reason in words, or as the hub sent it when this page does not know it yet. */
function useReason() {
  const t = useTranslations("kgUpload.reasons");
  return (reason: string) => (REASONS.has(reason) ? t(reason as "duplicate") : reason);
}

function levelOf(label: KgUpload["label"]): string | null {
  const level = (label as { level?: unknown } | null)?.level;
  return typeof level === "string" ? level : null;
}

function Counts({ counts, testId }: { counts: Record<string, number>; testId: string }) {
  const t = useTranslations("kgUpload.reports");
  const reason = useReason();
  const entries = Object.entries(counts).filter(([, count]) => count > 0);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  if (total === 0) return <span className="text-muted-foreground">{t("none")}</span>;
  return (
    <span className="flex flex-col gap-0.5" data-testid={testId} data-count={total}>
      <span className="tabular-nums">{total}</span>
      <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
        {entries.map(([key, count]) => (
          <li key={key} data-reason={key} className="tabular-nums">
            {t("reasonCount", { reason: reason(key), count })}
          </li>
        ))}
      </ul>
    </span>
  );
}

/**
 * One upload's report: what it is, then (read) the files read, skipped and kept without text by reason, and links. With
 * `pipeline`, the flow after it too, phase by phase, until the graph is ready or a phase failed.
 */
export function UploadReport({
  project,
  upload,
  initRun,
  pipeline = false,
}: {
  project: string;
  upload: KgUpload;
  initRun: KgInitRun | null;
  pipeline?: boolean;
}) {
  const t = useTranslations("kgUpload.reports");
  const size = useByteSize();
  const reason = useReason();
  const built = upload.state === "extracted" ? upload.build_status : null;
  const level = levelOf(upload.label);
  const paths = upload.paths ?? [];
  const questionsRun = (initRun?.questions as { agent_run_id?: unknown } | null | undefined)?.agent_run_id;
  return (
    <li className="flex flex-col gap-3 border-t px-4 py-4 first:border-t-0" data-testid="kg-upload-report" data-upload={upload.id} data-state={upload.state}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="truncate font-mono text-sm font-medium text-foreground" title={upload.name}>
              {upload.name}
            </span>
            {upload.state === "failed" ? <StatusPill look={FAILED} label={t("state.failed")} data-testid="kg-upload-state" /> : null}
            {built === "failed" ? <StatusPill look={FAILED} label={t("build.failed")} data-testid="kg-upload-build-state" /> : null}
            {MOVING.has(upload.state) ? (
              <span className="text-xs text-muted-foreground" data-testid="kg-upload-moving">
                {t(`state.${upload.state as "uploading"}`)}
              </span>
            ) : null}
            {built && BUILDING.has(built) ? (
              <span className="text-xs text-muted-foreground" data-testid="kg-upload-building">
                {t(`build.${built as "queued"}`)}
              </span>
            ) : null}
          </span>
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <Tag>upload.{upload.source}</Tag>
            {level ? <Tag>{t("label", { level })}</Tag> : null}
            <span className="tabular-nums">{size(upload.size)}</span>
            {upload.sender ? <span>{t("by", { login: upload.sender })}</span> : null}
            <Ago value={upload.created_at} never="" />
          </span>
        </div>
        <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
          {upload.build_id != null ? (
            <Link href={buildsHref(project)} className={LINK} data-testid="kg-upload-build-link">
              {t("buildLink", { id: upload.build_id })}
            </Link>
          ) : null}
          {initRun?.agent_run_id != null ? (
            <Link href={initRunHref(project, initRun.agent_run_id)} className={LINK} data-testid="kg-upload-init-link">
              {t("initLink", { id: initRun.agent_run_id })}
            </Link>
          ) : null}
          {typeof questionsRun === "number" ? (
            <Link href={initRunHref(project, questionsRun)} className={LINK} data-testid="kg-upload-questions-link">
              {t("questionsLink", { id: questionsRun })}
            </Link>
          ) : null}
        </span>
      </div>
      {pipeline ? <UploadPipeline project={project} upload={upload.id} /> : null}
      {upload.state === "failed" && upload.error ? (
        <p className="text-sm text-danger [overflow-wrap:anywhere]" data-testid="kg-upload-error">
          <span className="font-medium">{t("error")}: </span>
          {upload.error}
        </p>
      ) : null}
      {upload.state === "extracted" ? (
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3" data-testid="kg-upload-counts">
          <div className="flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">{t("files")}</dt>
            <dd className="tabular-nums" data-testid="kg-upload-files">
              {upload.files ?? 0}
            </dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">{t("skipped")}</dt>
            <dd>
              <Counts counts={upload.skipped} testId="kg-upload-skipped" />
            </dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">{t("assets")}</dt>
            <dd>
              <Counts counts={upload.assets ?? {}} testId="kg-upload-assets" />
            </dd>
          </div>
        </dl>
      ) : null}
      {paths.length > 0 ? (
        <details className="text-sm">
          <summary className="cursor-pointer text-[13px] font-medium text-brand">{t("paths", { count: paths.length })}</summary>
          <ul className="mt-2 flex flex-col gap-1" data-testid="kg-upload-paths">
            {paths.map((entry) => (
              <li key={`${entry.path}:${entry.reason}`} className="flex flex-wrap gap-x-2 text-xs">
                <span className="font-mono [overflow-wrap:anywhere]">{entry.path}</span>
                <span className="text-muted-foreground">{reason(entry.reason)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {upload.replaced_by != null ? <p className="text-xs text-muted-foreground">{t("replaced")}</p> : null}
    </li>
  );
}
