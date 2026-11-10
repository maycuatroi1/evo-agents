"use client";

import { ExternalLink, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

/**
 * An HTML version of a document (web/DESIGN.md, Docs): the page the hub serves from this origin at `url`
 * (GET /v1/x/docs/{project}/documents/{slug}/page?version=N), in a frame with `sandbox="allow-scripts allow-popups
 * allow-popups-to-escape-sandbox"` and never `allow-same-origin`, so it runs with an opaque origin, reads no cookie or
 * storage of the hub and cannot reach the page around it. The hub's answer carries a CSP of the same sandbox with
 * connect-src 'none' (docs/hub.md, Docs). Its links open in a new tab. The frame fills the window below the document's
 * head; Open page shows it alone in a tab, under the same CSP.
 */
export function PageFrame({ url, title }: { url: string; title: string }) {
  const t = useTranslations("docs.read");
  return (
    <div className="flex flex-col gap-2" data-testid="docs-page">
      <iframe
        src={url}
        title={t("pageFrame", { title })}
        sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
        referrerPolicy="no-referrer"
        className="h-[calc(100dvh-14rem)] min-h-[32rem] w-full rounded-md border border-border bg-white"
        data-testid="docs-page-frame"
      />
      <p className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="flex items-center gap-2">
          <ShieldCheck className="size-3.5 shrink-0" aria-hidden="true" />
          {t("pageNote")}
        </span>
        <Button asChild variant="link" size="sm" className="h-auto px-0">
          <a href={url} target="_blank" rel="noopener noreferrer" data-testid="docs-page-open">
            <ExternalLink aria-hidden="true" />
            {t("openPage")}
          </a>
        </Button>
      </p>
    </div>
  );
}
