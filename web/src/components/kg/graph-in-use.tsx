"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Ago } from "@/components/data/ago";
import { useByteSize } from "@/components/data/byte-size";
import { Fact, Facts } from "@/components/data/facts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { buildDurations, shortHash } from "@/lib/kg/format";
import type { KgBuild } from "@/lib/kg/types";

import { CopyButton, useDurationText } from "./badges";
import { ArtifactNote } from "./build-status";

function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="font-mono text-xs [overflow-wrap:anywhere]" title={title}>
      {children}
    </span>
  );
}

/** The graph the pages read: which build made it and when, its content hash, its artifact, its config and its times. */
export function GraphInUse({ graph, className }: { graph: KgBuild | undefined; className?: string }) {
  const t = useTranslations("kg.graphInUse");
  const bytes = useByteSize();
  const text = useDurationText();
  const { wait, run } = graph ? buildDurations(graph, null) : { wait: null, run: null };
  return (
    <section aria-labelledby="kg-graph-title" className={className}>
      <Card className="h-full" data-testid="kg-graph-in-use" data-build-id={graph?.id}>
        <CardHeader>
          <CardTitle>
            <h2 id="kg-graph-title">{t("title")}</h2>
          </CardTitle>
          <CardDescription>
            {graph ? (
              <>
                {t("build", { id: graph.id })}, {t("finished")} <Ago value={graph.finished_at} never="-" />
              </>
            ) : (
              t("none")
            )}
          </CardDescription>
        </CardHeader>
        {graph ? (
          <CardContent className="px-0">
            <Facts className="py-0">
              <Fact label={t("contentHash")}>
                {graph.content_hash ? (
                  <span className="flex min-w-0 items-start gap-1">
                    <span data-testid="graph-content-hash">
                      <Mono>{graph.content_hash}</Mono>
                    </span>
                    <CopyButton value={graph.content_hash} label={t("copyHash")} />
                  </span>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </Fact>
              <Fact label={t("artifact")}>
                <span className="flex flex-wrap items-center gap-x-2">
                  <ArtifactNote build={graph} now={null} />
                  {graph.artifact_sha256 ? (
                    <span className="text-xs text-muted-foreground">
                      <Mono title={graph.artifact_sha256}>{shortHash(graph.artifact_sha256)}</Mono>
                      {graph.artifact_size !== null ? <>, {bytes(graph.artifact_size)}</> : null}
                    </span>
                  ) : null}
                </span>
              </Fact>
              <Fact label={t("time")} testId="graph-time">
                {run !== null ? t("ran", { run: text(run), wait: wait === null ? "-" : text(wait) }) : "-"}
              </Fact>
              <Fact label={t("runs")}>
                <span className="tabular-nums">{graph.runs ?? "-"}</span>
              </Fact>
              <Fact label={t("config")}>
                {graph.config_digest ? <Mono title={graph.config_digest}>{shortHash(graph.config_digest)}</Mono> : "-"}
              </Fact>
            </Facts>
          </CardContent>
        ) : null}
      </Card>
    </section>
  );
}
