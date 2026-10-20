"use client";

import { useTranslations } from "next-intl";

import { Identifier, Tag } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

import { EvidenceOpener } from "./history-sessions";
import { useInsightFormat } from "./insight-format";
import { NUMBER, Panel, PanelLine, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";
import type { RuntimeNight, Timing } from "./runtime-model";

/**
 * The latest night's runtime and harness in detail, under the Runtime page's trends: each hook and the slowest tools
 * with their times, the always-loaded files by repo, the tools whose output filled the context most, the heaviest
 * sessions with their transcripts a click away, and the runs by kind.
 */

function Head({ children, numeric = false }: { children: string; numeric?: boolean }) {
  return (
    <th scope="col" className={cn(TABLE_HEAD, numeric && "text-right")}>
      {children}
    </th>
  );
}

function TimingTable({ label, rows, hooks, testId }: { label: string; rows: Timing[]; hooks: boolean; testId: string }) {
  const t = useTranslations("curator.runtime.tables");
  const format = useInsightFormat();
  const ms = (value: number | null) => (value === null ? <span className="text-fg-subtle">-</span> : format.ms(value));
  return (
    <PanelTable label={label} testId={testId}>
      <thead className="border-b bg-surface-sunken">
        <tr>
          <Head>{hooks ? t("hook") : t("tool")}</Head>
          <Head numeric>{t("timed")}</Head>
          <Head numeric>{t("p50")}</Head>
          <Head numeric>{t("p95")}</Head>
          <Head numeric>{t("max")}</Head>
          {hooks ? <Head numeric>{t("failed")}</Head> : null}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={`${row.name}-${row.command ?? ""}-${index}`} className="border-b last:border-b-0" data-testid={`${testId}-row`}>
            <th scope="row" className={cn(TABLE_CELL, "max-w-72 py-2 text-left font-normal")}>
              <span className="block font-mono text-xs text-foreground [overflow-wrap:anywhere]">{row.name}</span>
              {row.command ? (
                <span className="block truncate font-mono text-xs text-fg-subtle" title={row.command}>
                  {row.command}
                </span>
              ) : null}
            </th>
            <td className={cn(TABLE_CELL, NUMBER)}>{format.count(row.timed)}</td>
            <td className={cn(TABLE_CELL, NUMBER)}>{ms(row.p50)}</td>
            <td className={cn(TABLE_CELL, NUMBER, "text-foreground")}>{ms(row.p95)}</td>
            <td className={cn(TABLE_CELL, NUMBER)}>{ms(row.max)}</td>
            {hooks ? (
              <td className={cn(TABLE_CELL, NUMBER, row.failed + row.timedOut > 0 && "font-medium text-danger")}>
                {row.failed + row.timedOut > 0 ? t("failures", { failed: row.failed, timedOut: row.timedOut }) : "0"}
              </td>
            ) : null}
          </tr>
        ))}
      </tbody>
    </PanelTable>
  );
}

export function RuntimeDetails({ project, night }: { project: string; night: RuntimeNight }) {
  const t = useTranslations("curator.runtime.tables");
  const format = useInsightFormat();
  const day = format.day(night.night);
  const tokens = (value: number | null) => (value === null ? <span className="text-fg-subtle">-</span> : format.count(value));
  return (
    <div className="grid min-w-0 items-start gap-4 xl:grid-cols-2" data-testid="runtime-details">
      <Panel title={t("hooksTitle")} sub={t("hooksSub", { night: day })} testId="runtime-hooks">
        {night.hooks.length === 0 ? <PanelLine>{t("hooksEmpty")}</PanelLine> : <TimingTable label={t("hooksTitle")} rows={night.hooks} hooks testId="runtime-hooks-table" />}
      </Panel>
      <Panel title={t("toolsTitle")} sub={t("toolsSub", { night: day })} testId="runtime-tools">
        {night.tools.length === 0 ? <PanelLine>{t("toolsEmpty")}</PanelLine> : <TimingTable label={t("toolsTitle")} rows={night.tools} hooks={false} testId="runtime-tools-table" />}
      </Panel>
      <Panel
        title={t("loadedTitle")}
        sub={night.loaded.tokens !== null ? t("loadedSub", { files: night.loaded.files, tokens: format.count(night.loaded.tokens) }) : undefined}
        testId="runtime-loaded"
      >
        {night.loaded.repos.length === 0 ? (
          <PanelLine>{t("loadedEmpty")}</PanelLine>
        ) : (
          <ul className="flex flex-col divide-y" aria-label={t("loadedTitle")}>
            {night.loaded.repos.map((repo) => (
              <li key={repo.name} className="flex min-w-0 flex-col gap-1.5 px-4 py-2.5" data-testid="runtime-loaded-repo">
                <div className="flex min-w-0 flex-wrap items-center gap-2 text-[13px]">
                  <span className="font-mono text-xs font-medium text-foreground [overflow-wrap:anywhere]">{repo.name}</span>
                  {repo.dir ? <Tag>{t("folder")}</Tag> : null}
                  <span className="ml-auto text-xs text-muted-foreground tabular-nums">{t("tokens", { count: repo.tokens })}</span>
                </div>
                <ul className="flex flex-col gap-0.5">
                  {repo.files.map((file) => (
                    <li key={file.path} className="flex min-w-0 items-baseline gap-2 text-xs text-muted-foreground">
                      <span className="min-w-0 flex-1 truncate font-mono" title={file.path}>
                        {file.path}
                      </span>
                      <span className="shrink-0 tabular-nums">{t("fileLine", { tokens: format.count(file.tokens), sessions: file.sessions })}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={t("outputTitle")} sub={t("outputSub")} testId="runtime-output">
        {night.toolOutput.length === 0 ? (
          <PanelLine>{t("outputEmpty")}</PanelLine>
        ) : (
          <PanelTable label={t("outputTitle")} testId="runtime-output-table">
            <thead className="border-b bg-surface-sunken">
              <tr>
                <Head>{t("tool")}</Head>
                <Head numeric>{t("tokensHead")}</Head>
                <Head numeric>{t("sessions")}</Head>
              </tr>
            </thead>
            <tbody>
              {night.toolOutput.map((row) => (
                <tr key={row.tool} className="border-b last:border-b-0">
                  <th scope="row" className={cn(TABLE_CELL, "text-left font-normal")}>
                    <span className="inline-flex min-w-0 items-center gap-2">
                      <span className="font-mono text-xs text-foreground [overflow-wrap:anywhere]">{row.tool}</span>
                      {row.mcp ? <Tag>{t("mcp")}</Tag> : null}
                    </span>
                  </th>
                  <td className={cn(TABLE_CELL, NUMBER)}>{format.count(row.tokens)}</td>
                  <td className={cn(TABLE_CELL, NUMBER)}>{format.count(row.sessions)}</td>
                </tr>
              ))}
            </tbody>
          </PanelTable>
        )}
      </Panel>
      <Panel title={t("heaviestTitle")} sub={t("heaviestSub")} testId="runtime-heaviest">
        {night.heaviest.length === 0 ? (
          <PanelLine>{t("heaviestEmpty")}</PanelLine>
        ) : (
          <ul className="flex flex-col divide-y" aria-label={t("heaviestTitle")}>
            {night.heaviest.map((row) => (
              <li key={row.name} className="flex min-w-0 flex-col gap-1.5 px-4 py-2.5" data-testid="runtime-heavy">
                <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <Identifier value={row.name} title={row.name} className="max-w-56">
                    {row.name.slice(0, 13)}
                  </Identifier>
                  <span className="tabular-nums">{t("heavyLine", { tokens: format.count(row.tokens ?? 0), cache: format.count(row.cacheRead ?? 0) })}</span>
                  {row.peak !== null ? <span className="tabular-nums">{t("peak", { tokens: format.count(row.peak) })}</span> : null}
                  {row.cost !== null ? <span className="tabular-nums">{format.money(row.cost)}</span> : null}
                </div>
                {row.evidence ? <EvidenceOpener project={project} evidence={row.evidence} testId="runtime-heavy-open" /> : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={t("kindsTitle")} sub={t("kindsSub", { runs: night.runs.counted })} testId="runtime-kinds">
        {night.runs.byKind.length === 0 ? (
          <PanelLine>{t("kindsEmpty")}</PanelLine>
        ) : (
          <PanelTable label={t("kindsTitle")} testId="runtime-kinds-table">
            <thead className="border-b bg-surface-sunken">
              <tr>
                <Head>{t("kind")}</Head>
                <Head numeric>{t("runs")}</Head>
                <Head numeric>{t("tokensHead")}</Head>
                <Head numeric>{t("cost")}</Head>
              </tr>
            </thead>
            <tbody>
              {night.runs.byKind.map((row) => (
                <tr key={row.kind} className="border-b last:border-b-0">
                  <th scope="row" className={cn(TABLE_CELL, "text-left font-normal text-foreground")}>
                    {row.kind}
                  </th>
                  <td className={cn(TABLE_CELL, NUMBER)}>{format.count(row.runs)}</td>
                  <td className={cn(TABLE_CELL, NUMBER)}>{tokens(row.tokens)}</td>
                  <td className={cn(TABLE_CELL, NUMBER)}>{row.cost === null ? "-" : format.money(row.cost)}</td>
                </tr>
              ))}
            </tbody>
          </PanelTable>
        )}
      </Panel>
    </div>
  );
}
