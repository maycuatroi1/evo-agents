"use client";

import { Braces, Code2, ExternalLink, FileCode2, Folder, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { type Citation, type CodeLink, groupCode } from "@/lib/kg/entity-graph";
import { isWebLink } from "@/lib/kg/format";
import { nodeHref } from "@/lib/kg/routes";

import { SubTitle } from "./explorer-inspector";

const CODE_SHOWN = 8;

const CODE_ICONS: Record<string, LucideIcon> = { Directory: Folder, File: FileCode2, Symbol: Braces };

/** The document at the line the quote starts on, where the link is a web page that takes a line anchor. */
export function citationHref(citation: Pick<Citation, "uri" | "line">): string | null {
  if (!citation.uri || !isWebLink(citation.uri)) return null;
  return citation.line && !citation.uri.includes("#") ? `${citation.uri}#L${citation.line}` : citation.uri;
}

/** One code node an entity points to: where it is, why it is linked, how sure, and links to the code and its node page. */
function CodeItem({ project, link }: { project: string; link: CodeLink }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const format = useFormatter();
  const Icon = CODE_ICONS[link.kind] ?? FileCode2;
  const href = citationHref(link);
  const where = link.kind === "Symbol" ? link.path : null;
  return (
    <li className="flex flex-col gap-1 rounded-md border bg-muted/40 p-2.5" data-testid="kg-entity-code-link">
      <div className="flex min-w-0 items-start gap-1.5">
        <Icon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-label={link.kind} />
        <Link
          href={nodeHref(project, link.id)}
          className="min-w-0 font-mono text-[12px] font-medium [overflow-wrap:anywhere] text-foreground underline-offset-4 hover:underline"
          title={t("codeNodePage", { name: link.path ?? link.name })}
          data-testid="kg-entity-code-node"
        >
          {link.name}
        </Link>
        {link.conf === null ? null : (
          <span className="ml-auto shrink-0 text-[11px] text-fg-subtle tabular-nums" title={t("confidence")}>
            {format.number(link.conf, { style: "percent", maximumFractionDigits: 0 })}
          </span>
        )}
      </div>
      {where ? <p className="font-mono text-[11px] text-fg-subtle [overflow-wrap:anywhere]">{link.line ? `${where}:${link.line}` : where}</p> : null}
      {link.reason ? <p className="text-xs leading-5 [overflow-wrap:anywhere]">{link.reason}</p> : null}
      {link.name_match ? (
        <p className="text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]">{t("codeNameMatch", { name: link.name_match })}</p>
      ) : null}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-subtle">
        {link.rels.map((rel) => (
          <span key={rel} className="rounded-full border px-1.5 py-px font-mono" title={t(rel === "refers_to" ? "codeRefersTo" : "codeImplementedBy")}>
            {rel}
          </span>
        ))}
        {link.model ? <span className="font-mono">{link.model}</span> : null}
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex cursor-pointer items-center gap-1 text-brand underline-offset-4 hover:underline max-md:min-h-11"
            data-testid="kg-entity-code-source"
          >
            {t("openCode")}
            <ExternalLink className="size-3" aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </li>
  );
}

/** The Directories, Files and Symbols the entity points to (implemented_by, refers_to), grouped by repo and package. */
export function CodeLinks({ project, links, total }: { project: string; links: CodeLink[]; total: number }) {
  const t = useTranslations("kg.explorer.entities.inspector");
  const [all, setAll] = useState(false);
  const groups = groupCode(all ? links : links.slice(0, CODE_SHOWN));
  return (
    <section className="flex flex-col gap-2" data-testid="kg-entity-code" aria-labelledby="kg-entity-code-title">
      <SubTitle>
        <span id="kg-entity-code-title" className="inline-flex items-center gap-1.5">
          <Code2 className="size-3.5" aria-hidden="true" />
          {t("code", { count: total })}
        </span>
      </SubTitle>
      {links.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("noCode")}</p>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map((group) => (
            <div key={group.key} className="flex flex-col gap-1.5" data-testid="kg-entity-code-package">
              <p className="flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
                <Folder className="size-3 shrink-0" aria-hidden="true" />
                <span className="[overflow-wrap:anywhere]">{group.key}</span>
              </p>
              <ul className="flex flex-col gap-1.5">
                {group.links.map((link) => (
                  <CodeItem key={link.id} project={project} link={link} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      {links.length > CODE_SHOWN ? (
        <Button type="button" variant="ghost" size="sm" className="w-fit cursor-pointer" onClick={() => setAll(!all)}>
          {all ? t("showFewer") : t("showAll", { count: links.length })}
        </Button>
      ) : null}
      {total > links.length ? <p className="text-[11px] text-fg-subtle">{t("codeMore", { shown: links.length, total })}</p> : null}
    </section>
  );
}
