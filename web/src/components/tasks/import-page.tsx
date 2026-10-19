"use client";

import { CircleCheck, CircleX, Eye, FileUp, Lock, TriangleAlert, Upload } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ChangeEvent, type FormEvent, useId, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { importTasks, type TasksImport, type TasksImportResult } from "./api";
import { TasksTabs } from "./chrome";
import { signInAgainOn401, useTasksRefresh, useTasksViewer } from "./hooks";
import { backlogHref, boardHref } from "./links";
import { canWrite } from "./model";

const MAX_BYTES = 8 * 1024 * 1024; // what the page reads into the box; the hub caps the tasks a file holds

/** The text as JSON, or why it is not. */
function parse(text: string): { data: TasksImport["data"] | null; error: string | null } {
  try {
    const data: unknown = JSON.parse(text);
    if (!data || typeof data !== "object" || Array.isArray(data)) return { data: null, error: "object" };
    return { data: data as TasksImport["data"], error: null };
  } catch (error) {
    return { data: null, error: error instanceof Error ? error.message : String(error) };
  }
}

/** What an import holds and would make, as the hub's dry run says: counts, the tasks it skips, its problems. */
function Preview({ result }: { result: TasksImportResult }) {
  const t = useTranslations("tasks.import");
  const errors = result.problems.filter((problem) => problem.level === "error").length;
  return (
    <Card size="sm" data-testid="tasks-import-preview">
      <CardHeader>
        <CardTitle>
          <h2>{t("previewTitle")}</h2>
        </CardTitle>
        <p className={cn("flex items-center gap-1.5 text-xs", errors ? "text-danger" : "text-success")} role="status" data-testid="tasks-import-verdict">
          {errors ? <CircleX className="size-3.5" aria-hidden="true" /> : <CircleCheck className="size-3.5" aria-hidden="true" />}
          {errors ? t("refused", { count: errors }) : t("ready")}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-1.5" data-testid="tasks-import-counts">
          <Tag>{t("tasks", { count: result.tasks })}</Tag>
          <Tag>{t("sprints", { count: result.sprints })}</Tag>
          <Tag>{t("links", { count: result.links })}</Tag>
        </div>
        {result.skipped.length ? (
          <div className="flex flex-col gap-1" data-testid="tasks-import-skipped">
            <h3 className="text-xs font-medium text-muted-foreground">{t("skipped", { count: result.skipped.length })}</h3>
            <p className="text-[13px] [overflow-wrap:anywhere]">{result.skipped.map((skip) => t("skip", { key: skip.key, task: skip.task })).join(", ")}</p>
          </div>
        ) : null}
        {result.problems.length ? (
          <div className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-muted-foreground">{t("problems", { count: result.problems.length })}</h3>
            <ul className="flex flex-col gap-1.5" role="list" data-testid="tasks-import-problems">
              {result.problems.map((problem, index) => {
                const error = problem.level === "error";
                const Icon = error ? CircleX : TriangleAlert;
                return (
                  <li key={index} className="flex items-start gap-2 text-[13px]" data-level={problem.level}>
                    <Icon className={cn("mt-0.5 size-4 shrink-0", error ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="sr-only">{error ? t("error") : t("warning")}: </span>
                      {problem.task ? <span className="mr-1.5 font-mono text-xs text-fg-subtle">{problem.task}</span> : null}
                      {problem.message}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : (
          <p className="text-[13px] text-success">{t("noProblems")}</p>
        )}
      </CardContent>
    </Card>
  );
}

function ImportForm({ project }: { project: string }) {
  const t = useTranslations("tasks.import");
  const id = useId();
  const router = useRouter();
  const failure = useWriteFailure();
  const refresh = useTasksRefresh(project);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<{ for: string; result: TasksImportResult } | null>(null);
  const [busy, setBusy] = useState<"preview" | "import" | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const parsed = text.trim() ? parse(text) : { data: null, error: null };
  const current = preview && preview.for === text ? preview.result : null;
  const tooBig = new Blob([text]).size > MAX_BYTES;
  const blocked = !parsed.data || tooBig;
  const readFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setText(await file.text());
    event.target.value = "";
  };
  const run = async (dryRun: boolean) => {
    if (blocked || busy || !parsed.data) return;
    setBusy(dryRun ? "preview" : "import");
    setRefused(null);
    try {
      const result = await importTasks(browserApi(), project, { data: parsed.data, dry_run: dryRun });
      if (dryRun) {
        setPreview({ for: text, result });
        return;
      }
      await refresh();
      notify({ tone: "success", text: t("imported", { count: result.created.length }), description: result.skipped.length ? t("skippedText", { count: result.skipped.length }) : null });
      router.push(backlogHref(project));
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(null);
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(current === null);
  };
  const hint = tooBig ? t("tooBig") : parsed.error ? (parsed.error === "object" ? t("notObject") : t("notJson", { error: parsed.error })) : t("jsonHint");
  return (
    <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_24rem]">
      <form onSubmit={submit} className="flex min-w-0 flex-col gap-4 rounded-md border bg-card p-4 md:p-6" aria-label={t("formLabel")} data-testid="tasks-import-form">
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Label htmlFor={id}>{t("json")}</Label>
            <label className="inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-sm px-2 text-[13px] font-medium text-brand hover:bg-accent max-md:min-h-11">
              <FileUp className="size-4" aria-hidden="true" />
              {t("readFile")}
              <input type="file" accept=".json,application/json" className="sr-only" onChange={(event) => void readFile(event)} data-testid="tasks-import-file" />
            </label>
          </div>
          <Textarea
            id={id}
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={18}
            spellCheck={false}
            aria-describedby={`${id}-hint`}
            aria-invalid={parsed.error || tooBig ? true : undefined}
            className="max-h-[32rem] font-mono text-[13px] leading-5"
            data-testid="tasks-import-json"
          />
          <p id={`${id}-hint`} className={parsed.error || tooBig ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
            {hint}
          </p>
        </div>
        {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <p className="mr-auto text-xs text-fg-subtle">{current ? t("previewed") : t("previewFirst")}</p>
          <Button type="button" variant="secondary" busy={busy === "preview"} disabled={blocked} onClick={() => void run(true)} data-testid="tasks-import-preview-run">
            <Eye aria-hidden="true" />
            {t("preview")}
          </Button>
          <Button
            type="button"
            busy={busy === "import"}
            disabled={blocked || current === null || current.problems.some((problem) => problem.level === "error") || current.tasks === 0}
            onClick={() => void run(false)}
            data-testid="tasks-import-run"
          >
            <Upload aria-hidden="true" />
            {t("import")}
          </Button>
        </div>
      </form>
      <div className="flex min-w-0 flex-col gap-4">
        {current ? (
          <Preview result={current} />
        ) : (
          <Banner icon={Eye} title={t("noPreviewTitle")}>
            {t("noPreviewText")}
          </Banner>
        )}
      </div>
    </div>
  );
}

/**
 * Tasks into a project from evo-tasks/1 JSON, once (/p/{project}/x/tasks/import, a page without a menu item): its
 * sprints and tasks, linked to SRS items by code, to plan steps and pull requests. The hub's dry run previews what it
 * would make and what is wrong first; a task imported before from the same source is skipped. Importing takes the
 * writer role.
 */
export function ImportPage({ project, title }: { project: string; title: string }) {
  const t = useTranslations("tasks.import");
  const viewer = useTasksViewer(project);
  const writer = canWrite(viewer);
  return (
    <div className="flex flex-col gap-6" data-testid="tasks-import">
      <div className="flex flex-col gap-4">
        <PageHeader title={title} sub={t("sub")} />
        <TasksTabs project={project} current="import" writer={writer} />
      </div>
      {viewer && !writer ? (
        <Banner icon={Lock} title={t("readerTitle", { project })} testId="tasks-import-reader">
          <Link href={boardHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
            {t("backToBoard")}
          </Link>
        </Banner>
      ) : viewer ? (
        <ImportForm project={project} />
      ) : null}
    </div>
  );
}
