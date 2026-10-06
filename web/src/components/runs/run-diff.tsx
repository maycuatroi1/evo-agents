"use client";

import { ArrowLeft, CircleCheck, Download, FileQuestion, ServerOff, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { InlineError, type WriteFailure } from "@/components/admin/notice";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton, ServerErrorState, StatePanel } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { type DiffFile, type DiffHunk, type DiffLine, fileAnchor, MAX_DIFF_LINES, type ParsedDiff } from "./diff-model";
import { useRunFailure } from "./hooks";
import { type Run, runDiffLink, runHref, runQuery } from "./queries";

/** What the server found of the run's diff (page.tsx of the diff route). */
export type DiffState =
  | { status: "ok"; diff: ParsedDiff; bytes: number; cutBytes: boolean; maxBytes: number; sha256: string }
  | { status: "none" }
  | { status: "unavailable" }
  | { status: "unreadable"; reason: string; sha256: string }
  | { status: "error"; error: ApiErrorInfo };

const ROW_TONE: Record<DiffLine["kind"], string> = {
  context: "",
  added: "bg-success-soft",
  removed: "bg-danger-soft",
  note: "",
};
const SIGN: Record<DiffLine["kind"], string> = { context: " ", added: "+", removed: "-", note: "\\" };
const SIGN_TONE: Record<DiffLine["kind"], string> = {
  context: "text-muted-foreground",
  added: "text-success",
  removed: "text-danger",
  note: "text-muted-foreground",
};
const NUMBER = "w-0 px-2 py-0.5 text-right align-top font-mono text-xs text-muted-foreground tabular-nums select-none";
const CODE =
  "px-2 py-0.5 align-top font-mono text-[13px] leading-relaxed whitespace-pre-wrap [font-variant-ligatures:none] [overflow-wrap:anywhere]";

const STATUS_VARIANT: Record<DiffFile["status"], "success" | "destructive" | "info" | "secondary"> = {
  added: "success",
  deleted: "destructive",
  renamed: "info",
  copied: "info",
  modified: "secondary",
};

function Hunk({ hunk, index, path }: { hunk: DiffHunk; index: number; path: string }) {
  const t = useTranslations("runs.diff");
  return (
    <div className="border-t first:border-t-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 bg-muted/60 px-3 py-1.5">
        <span className="font-mono text-xs text-muted-foreground [font-variant-ligatures:none]">
          @@ -{hunk.oldStart},{hunk.oldLines} +{hunk.newStart},{hunk.newLines} @@
        </span>
        {hunk.section ? <span className="truncate font-mono text-xs text-muted-foreground">{hunk.section}</span> : null}
      </div>
      <table className="w-full border-collapse">
        <caption className="sr-only">{t("hunkCaption", { index: index + 1, path, from: hunk.newStart })}</caption>
        <thead className="sr-only">
          <tr>
            <th scope="col">{t("oldLine")}</th>
            <th scope="col">{t("newLine")}</th>
            <th scope="col">{t("text")}</th>
          </tr>
        </thead>
        <tbody>
          {hunk.lines.map((line, row) => (
            <tr key={row} className={ROW_TONE[line.kind]} data-kind={line.kind} data-testid="diff-line">
              <td className={NUMBER}>{line.old ?? ""}</td>
              <td className={cn(NUMBER, "border-r")}>{line.new ?? ""}</td>
              <td className={cn(CODE, line.kind === "note" && "text-muted-foreground italic")}>
                {line.kind === "added" || line.kind === "removed" ? (
                  <span className="sr-only">{line.kind === "added" ? t("added") : t("removed")} </span>
                ) : null}
                <span className={cn("mr-2 inline-block w-2 font-semibold", SIGN_TONE[line.kind])} aria-hidden="true">
                  {SIGN[line.kind]}
                </span>
                {line.text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FileCounts({ file }: { file: DiffFile }) {
  const t = useTranslations("runs.diff");
  return (
    <span className="shrink-0 font-mono text-xs tabular-nums">
      <span className="text-success">+{file.additions}</span>{" "}
      <span className="text-danger">-{file.deletions}</span>
      <span className="sr-only">{t("fileCounts", { additions: file.additions, deletions: file.deletions })}</span>
    </span>
  );
}

function FileSection({ file, index }: { file: DiffFile; index: number }) {
  const t = useTranslations("runs.diff");
  return (
    <section
      id={fileAnchor(index)}
      aria-labelledby={`${fileAnchor(index)}-title`}
      className="scroll-mt-20 overflow-hidden rounded-md border bg-card shadow-raised [contain-intrinsic-size:auto_480px] [content-visibility:auto]"
      data-testid="diff-file"
      data-path={file.path}
    >
      <details open className="group">
        <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
          <h2 id={`${fileAnchor(index)}-title`} className="min-w-0 flex-1 font-mono text-sm font-medium break-all">
            {file.from ? (
              <>
                <span className="text-muted-foreground">{file.from}</span> <span aria-hidden="true">{"→"}</span>
                <span className="sr-only">{t("renamedTo")}</span> {file.path}
              </>
            ) : (
              file.path
            )}
          </h2>
          <Badge variant={STATUS_VARIANT[file.status]}>{t(`status.${file.status}`)}</Badge>
          <FileCounts file={file} />
          <span className="text-xs text-muted-foreground group-open:hidden">{t("show")}</span>
          <span className="hidden text-xs text-muted-foreground group-open:inline">{t("hide")}</span>
        </summary>
        <div className="border-t">
          {file.binary ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">{t("binary")}</p>
          ) : file.hunks.length === 0 ? (
            <p className="px-4 py-3 text-sm text-muted-foreground">{file.modeOnly ? t("modeOnly") : t("noText")}</p>
          ) : (
            file.hunks.map((hunk, position) => <Hunk key={position} hunk={hunk} index={position} path={file.path} />)
          )}
        </div>
      </details>
    </section>
  );
}

function DownloadButton({ run }: { run: Pick<Run, "project" | "id"> }) {
  const t = useTranslations("runs.diff");
  const failure = useRunFailure();
  const [state, setState] = useState<"idle" | "pending" | "started">("idle");
  const [error, setError] = useState<WriteFailure | null>(null);
  const start = async () => {
    if (state === "pending") return;
    setState("pending");
    setError(null);
    try {
      const link = await runDiffLink(browserApi(), run.project, run.id, true);
      window.location.assign(link.url); // the store answers with an attachment, so the page stays
      setState("started");
    } catch (cause) {
      setError(failure(cause));
      setState("idle");
    }
  };
  return (
    <div className="relative flex flex-col items-end gap-1.5">
      <Button type="button" variant="outline" onClick={() => void start()} busy={state === "pending"} data-testid="diff-download">
        <Download aria-hidden="true" />
        {t("download")}
      </Button>
      {/* Under the button without taking room, so the header's row keeps its height. */}
      <span aria-live="polite" className="absolute top-full right-0 mt-1 text-xs whitespace-nowrap text-muted-foreground">
        {state === "started" ? (
          <span className="inline-flex items-center gap-1">
            <CircleCheck className="size-3.5" aria-hidden="true" />
            {t("downloadStarted", { file: `run-${run.id}.diff` })}
          </span>
        ) : null}
      </span>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
    </div>
  );
}

function DiffBody({ run, diff }: { run: Run; diff: DiffState }) {
  const t = useTranslations("runs.diff");
  const format = useFormatter();
  if (diff.status === "none") {
    return (
      <StatePanel icon={FileQuestion} title={t("none.title")} description={t("none.description")} testId="diff-none">
        <Button asChild variant="outline">
          <Link href={runHref(run.project, run.id)}>{t("back")}</Link>
        </Button>
      </StatePanel>
    );
  }
  if (diff.status === "unavailable") {
    return <StatePanel icon={ServerOff} tone="warning" title={t("unavailable.title")} description={t("unavailable.description")} testId="diff-unavailable" />;
  }
  if (diff.status === "error") return <ServerErrorState error={diff.error} />;
  if (diff.status === "unreadable") {
    return (
      <StatePanel icon={TriangleAlert} tone="danger" title={t("unreadable.title")} description={t("unreadable.description")} testId="diff-unreadable">
        <DownloadButton run={run} />
      </StatePanel>
    );
  }
  const { files, additions, deletions, cut } = diff.diff;
  return (
    <div className="flex flex-col gap-4">
      {cut || diff.cutBytes ? (
        <p className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention" data-testid="diff-cut">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span className="text-pretty">{t("cut", { lines: format.number(MAX_DIFF_LINES) })}</span>
        </p>
      ) : null}
      {files.length === 0 ? (
        <p className="rounded-md border border-dashed bg-card px-4 py-6 text-center text-sm text-muted-foreground" data-testid="diff-empty">
          {t("empty")}
        </p>
      ) : (
        <>
          <nav aria-label={t("filesLabel")} className="rounded-md border bg-card shadow-raised" data-testid="diff-files">
            <h2 className="border-b px-4 py-3 text-base font-medium">
              {t("filesTitle", { count: files.length })}{" "}
              <span className="font-mono text-sm font-normal tabular-nums">
                <span className="text-success">+{format.number(additions)}</span>{" "}
                <span className="text-danger">-{format.number(deletions)}</span>
              </span>
            </h2>
            <ol className="flex max-h-72 flex-col divide-y overflow-y-auto">
              {files.map((file, index) => (
                <li key={index} className="flex min-w-0 items-center gap-3 px-4 py-2">
                  <a href={`#${fileAnchor(index)}`} className="min-w-0 flex-1 font-mono text-xs break-all text-brand underline-offset-4 hover:underline">
                    {file.path}
                  </a>
                  <Badge variant={STATUS_VARIANT[file.status]}>{t(`status.${file.status}`)}</Badge>
                  <FileCounts file={file} />
                </li>
              ))}
            </ol>
          </nav>
          {files.map((file, index) => (
            <FileSection key={index} file={file} index={index} />
          ))}
        </>
      )}
    </div>
  );
}

/** The diff of a run's commits, file by file, with the counts of each and a download of the whole patch. */
export function RunDiff({ project, runId, initialError, diff }: { project: string; runId: number; initialError: ApiErrorInfo | null; diff: DiffState }) {
  const t = useTranslations("runs.diff");
  const tDetail = useTranslations("runs.detail");
  const format = useFormatter();
  const state = useHubQuery(runQuery(browserApi, project, runId), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={tDetail("notFoundTitle", { id: runId })} description={tDetail("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(run) => (
        <div className="flex flex-col gap-6" data-testid="run-diff" data-run-id={run.id}>
          <PageHeader
            eyebrow={<span className="font-mono normal-case">{run.project}</span>}
            title={
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <span>
                  {t("title")} <span className="font-mono tabular-nums">#{run.id}</span>
                </span>
                <StatusBadge kind="run" status={run.state} size="lg" />
              </span>
            }
            description={
              diff.status === "ok"
                ? t("description", {
                    title: run.title ?? tDetail("untitled"),
                    files: diff.diff.files.length,
                    size: format.number(diff.bytes / 1024, { maximumFractionDigits: 1 }),
                  })
                : (run.title ?? tDetail("untitled"))
            }
            meta={
              <>
                <Button asChild variant="ghost">
                  <Link href={runHref(run.project, run.id)} data-testid="diff-back">
                    <ArrowLeft aria-hidden="true" />
                    {t("back")}
                  </Link>
                </Button>
                {diff.status === "ok" ? <DownloadButton run={run} /> : null}
              </>
            }
          />
          <DiffBody run={run} diff={diff} />
        </div>
      )}
    </QueryView>
  );
}
