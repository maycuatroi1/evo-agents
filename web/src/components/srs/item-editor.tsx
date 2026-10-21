"use client";

import { useQueryClient } from "@tanstack/react-query";
import { GitMerge, Save, TriangleAlert, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type SrsChange, type SrsDocument, type SrsItem, saveVersion, srsDocumentQuery } from "./api";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { isRow } from "./model";

/** One item of a 409: the text the member opened and the text the latest version holds now. */
type Conflict = { item: string | null; code: string | null; base: string; latest: string };

function conflictOf(error: unknown, key: string): Conflict | null {
  if (!isApiError(error) || error.status !== 409) return null;
  const found = (error.info.detail ?? []).find((entry) => typeof entry === "object" && entry && (entry as Conflict).item === key);
  return (found as Conflict | undefined) ?? null;
}

function problemsOf(error: unknown): string[] {
  if (!isApiError(error) || error.status !== 422) return [];
  return (error.info.detail ?? [])
    .map((entry) => (typeof entry === "object" && entry && typeof (entry as { message?: unknown }).message === "string" ? (entry as { message: string }).message : null))
    .filter((message): message is string => message !== null);
}

function screenKeys(item: SrsItem): string[] {
  return item.refs.filter((ref) => ref.rel === "uses_screen").map((ref) => ref.to);
}

/** The screens a use case links, as checkboxes. */
function ScreenPicker({ screens, value, onChange }: { screens: SrsItem[]; value: Set<string>; onChange: (next: Set<string>) => void }) {
  const t = useTranslations("srs.editor");
  if (screens.length === 0) return null;
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium">{t("screens")}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {screens.map((screen) => (
          <label key={screen.key} className="inline-flex min-h-8 items-center gap-2 text-sm max-md:min-h-11">
            <input
              type="checkbox"
              className="size-4 shrink-0 cursor-pointer accent-primary"
              checked={value.has(screen.key)}
              onChange={(event) => {
                const next = new Set(value);
                if (event.target.checked) next.add(screen.key);
                else next.delete(screen.key);
                onChange(next);
              }}
            />
            <span className="font-mono text-xs text-muted-foreground">{screen.code}</span>
            {screen.title}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/**
 * The editor of one item, over the document's latest version: its title (a heading's), its Markdown (a table row's
 * line), a use case's screens, and what the version changes. Saving makes the next version, a draft built on the
 * latest. When someone saved the same item since the version the member opened, the hub refuses (409) and the editor
 * shows their text beside the member's, to merge by hand and save again on the newer version.
 */
export function ItemEditor({
  project,
  latest,
  itemKey,
  cancelHref,
  onSaved,
}: {
  project: string;
  /** The document with its latest version whole. */
  latest: SrsDocument;
  itemKey: string;
  cancelHref: Route;
  onSaved: (version: number) => void;
}) {
  const t = useTranslations("srs.editor");
  const ids = useId();
  const queryClient = useQueryClient();
  const refresh = useDocumentRefresh(project, latest.slug);
  const failure = useWriteFailure();
  const items = useMemo(() => latest.shown?.items ?? [], [latest.shown]);
  const item = items.find((one) => one.key === itemKey) ?? null;
  const [base, setBase] = useState(latest.latest_version ?? 1);
  const [title, setTitle] = useState(item?.title ?? "");
  const [body, setBody] = useState(item?.body ?? "");
  const [summary, setSummary] = useState("");
  const [screens, setScreens] = useState(() => new Set(item ? screenKeys(item) : []));
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [refused, setRefused] = useState<{ failure: WriteFailure; problems: string[] } | null>(null);

  if (!item) {
    return (
      <Banner icon={TriangleAlert} tone="attention" title={t("goneTitle", { version: latest.latest_version ?? 0 })} testId="srs-editor-gone">
        <Link href={cancelHref} className="text-brand underline underline-offset-4 hover:text-brand-hover">
          {t("cancel")}
        </Link>
      </Banner>
    );
  }
  const row = isRow(item);
  const allScreens = items.filter((one) => one.kind === "screen");
  const linked = screenKeys(item);
  const screensChanged = item.kind === "use_case" && (linked.length !== screens.size || linked.some((key) => !screens.has(key)));
  const change: SrsChange = {
    key: item.key,
    retire: false,
    ...(!row && title.trim() && title.trim() !== item.title ? { title: title.trim() } : {}),
    ...(body !== item.body ? { body } : {}),
    ...(screensChanged
      ? {
          refs: [
            ...item.refs.filter((ref) => ref.rel !== "uses_screen").map((ref) => ({ rel: ref.rel, to: ref.to })),
            ...[...screens].map((key) => ({ rel: "uses_screen" as const, to: key })),
          ],
        }
      : {}),
  };
  const changed = change.title !== undefined || change.body !== undefined || change.refs !== undefined;
  const next = (latest.latest_version ?? base) + 1;

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!changed || busy) return;
    setBusy(true);
    setRefused(null);
    try {
      const saved = await saveVersion(browserApi(), project, latest.slug, { base_version: base, summary: summary.trim(), changes: [change] });
      await refresh();
      notify({ tone: "success", text: t("saved", { version: saved.number }), description: t("savedText") });
      onSaved(saved.number);
    } catch (error) {
      signInAgainOn401(error);
      const theirs = conflictOf(error, item.key);
      if (theirs) {
        await refresh();
        const now = await queryClient.fetchQuery(srsDocumentQuery(browserApi, project, latest.slug, null));
        setConflict(theirs);
        setBase(now.latest_version ?? base);
      } else {
        setRefused({ failure: failure(error), problems: problemsOf(error) });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(event) => void save(event)} className="flex flex-col gap-4 rounded-md border bg-card p-4" aria-labelledby={`${ids}-title`} data-testid="srs-editor">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
          {t("title", { name: item.code ?? item.title })}
        </h2>
        <p className="text-xs text-fg-subtle">{t("makes", { next, base })}</p>
      </div>
      {conflict ? (
        <Banner icon={GitMerge} tone="attention" role="alert" title={t("conflictTitle", { version: base })} testId="srs-editor-conflict">
          <p>{t("conflictText", { version: base })}</p>
          <pre className="mt-2 max-h-56 overflow-auto rounded-sm border bg-card p-2 font-mono text-xs whitespace-pre-wrap text-foreground" tabIndex={0} aria-label={t("theirText")}>
            {conflict.latest}
          </pre>
        </Banner>
      ) : null}
      {row ? null : (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-name`}>{t("itemTitle")}</Label>
          <Input id={`${ids}-name`} value={title} onChange={(event) => setTitle(event.target.value)} maxLength={500} required data-testid="srs-editor-title" />
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-body`}>{row ? t("rowLine") : t("body")}</Label>
        <Textarea
          id={`${ids}-body`}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={row ? 2 : 12}
          spellCheck={false}
          aria-describedby={`${ids}-body-hint`}
          className="font-mono text-[13px] leading-5"
          data-testid="srs-editor-body"
        />
        <p id={`${ids}-body-hint`} className="text-xs text-fg-subtle">
          {row ? t("rowHint") : item.kind === "use_case" ? t("useCaseHint") : t("bodyHint")}
        </p>
      </div>
      {item.kind === "use_case" ? <ScreenPicker screens={allScreens} value={screens} onChange={setScreens} /> : null}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-summary`}>{t("summary")}</Label>
        <Input id={`${ids}-summary`} value={summary} onChange={(event) => setSummary(event.target.value)} maxLength={500} placeholder={t("summaryPlaceholder")} data-testid="srs-editor-summary" />
      </div>
      {refused ? (
        <div className="flex flex-col gap-2">
          <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} />
          {refused.problems.length ? (
            <ul className="list-disc pl-6 text-[13px] text-danger" data-testid="srs-editor-problems">
              {refused.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {changed ? null : <span className="mr-auto text-xs text-fg-subtle">{t("unchanged")}</span>}
        <Button asChild variant="secondary">
          <Link href={cancelHref} scroll={false} data-testid="srs-editor-cancel">
            <X aria-hidden="true" />
            {t("cancel")}
          </Link>
        </Button>
        <Button type="submit" busy={busy} disabled={!changed} data-testid="srs-editor-save">
          <Save aria-hidden="true" />
          {busy ? t("saving") : t("save", { version: next })}
        </Button>
      </div>
    </form>
  );
}
