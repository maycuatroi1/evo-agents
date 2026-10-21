"use client";

import { GitCommitHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SourceRead } from "@/lib/kg/types";

const SHORT_SHA = 7;

/** How far the commit read is behind the remote's default branch: a badge only when it is behind. */
function Behind({ read }: { read: SourceRead }) {
  const t = useTranslations("kg.commits");
  if (read.base == null || read.behind == null) {
    return <span className="text-muted-foreground">{t("notCompared")}</span>;
  }
  if (read.behind === 0) {
    return (
      <span className="text-muted-foreground" data-testid="source-behind" data-behind={0}>
        {t("upToDate", { base: read.base })}
      </span>
    );
  }
  return (
    <Badge variant="warning" data-testid="source-behind" data-behind={read.behind}>
      <GitCommitHorizontal aria-hidden="true" />
      {t("behind", { count: read.behind, base: read.base })}
    </Badge>
  );
}

/**
 * The commit each git source of the graph read, as the machine that synced it counted how far behind its remote's
 * default branch that was. Nothing when no source the member can see read a commit.
 */
export function SourceCommits({ sources }: { sources: readonly SourceRead[] }) {
  const t = useTranslations("kg.commits");
  if (sources.length === 0) return null;
  return (
    <section aria-labelledby="kg-commits-title">
      <Card data-testid="kg-commits">
        <CardHeader>
          <CardTitle>
            <h2 id="kg-commits-title">{t("title")}</h2>
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col divide-y" aria-label={t("title")}>
            {sources.map((read) => {
              const short = read.commit.slice(0, SHORT_SHA);
              return (
                <li
                  key={read.source}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm first:pt-0 last:pb-0"
                  data-testid="source-commit"
                  data-source={read.source}
                >
                  <span className="font-medium [overflow-wrap:anywhere]">{read.source}</span>
                  <Identifier
                    value={read.commit}
                    externalHref={read.url}
                    linkLabel={t("open", { commit: short })}
                    title={read.commit}
                    copy
                    copyLabel={t("copy", { commit: short })}
                    testId="source-commit-sha"
                  >
                    {short}
                  </Identifier>
                  <span className="text-muted-foreground">{t("ref", { ref: read.ref ?? "HEAD" })}</span>
                  <Behind read={read} />
                </li>
              );
            })}
          </ul>
        </CardContent>
      </Card>
    </section>
  );
}
