"use client";

import { ChevronDown, ListTree } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import type { SrsDiffItem, SrsItem } from "./api";
import { type OutlineNode, outline } from "./model";

type Props = {
  items: SrsItem[];
  selected: string | null;
  changes: ReadonlyMap<string, SrsDiffItem["change"]>;
  hrefOf: (key: string) => Route;
};

/** The code and the title in one run of text, which wraps between words in a narrow column. */
function Label({ item }: { item: SrsItem }) {
  return (
    <span className="min-w-0 break-words">
      {item.code ? <span className="mr-1.5 font-mono text-xs whitespace-nowrap text-muted-foreground">{item.code}</span> : null}
      {item.title}
    </span>
  );
}

function Branch({ nodes, depth, ...props }: Props & { nodes: OutlineNode[]; depth: number }) {
  const t = useTranslations("srs.toc");
  return (
    <ul className={cn("flex flex-col", depth > 0 && "ml-3 border-l pl-2")} role="list">
      {nodes.map(({ item, children }) => {
        const current = item.key === props.selected;
        const change = props.changes.get(item.key);
        return (
          <li key={item.key}>
            <Link
              href={props.hrefOf(item.key)}
              scroll={false}
              aria-current={current ? "true" : undefined}
              data-item-key={item.key}
              className={cn(
                "flex min-h-8 items-start gap-1.5 rounded-sm px-2 py-1.5 text-[13px] leading-[18px] transition-colors max-md:min-h-11",
                current ? "bg-surface-selected font-medium text-foreground" : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <Label item={item} />
              {change ? (
                <span className="mt-1 ml-auto size-2 shrink-0 rounded-full bg-review-solid" title={t(`changed.${change}`)}>
                  <span className="sr-only">{t(`changed.${change}`)}</span>
                </span>
              ) : null}
            </Link>
            {children.length ? <Branch nodes={children} depth={depth + 1} {...props} /> : null}
          </li>
        );
      })}
    </ul>
  );
}

/** The document's contents as a tree of links, the open item marked, and a dot on each item the version changed. */
export function Toc(props: Props) {
  const t = useTranslations("srs.toc");
  return (
    <nav aria-label={t("label")} className="min-w-0" data-testid="srs-toc">
      <Branch nodes={outline(props.items)} depth={0} {...props} />
    </nav>
  );
}

/**
 * The contents folded under one button, for screens too narrow for the tree beside the text: the open item's name on
 * the button, the same tree inside.
 */
export function TocDisclosure(props: Props) {
  const t = useTranslations("srs.toc");
  const current = props.items.find((item) => item.key === props.selected);
  return (
    <details className="group rounded-md border bg-card" data-testid="srs-toc-disclosure">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <ListTree className="size-4 text-muted-foreground" aria-hidden="true" />
        <span>{t("label")}</span>
        {current ? (
          <span className="min-w-0 truncate font-normal text-muted-foreground">{current.code ?? current.title}</span>
        ) : null}
        <ChevronDown className="ml-auto size-4 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>
      <nav aria-label={t("label")} className="border-t p-2">
        <Branch nodes={outline(props.items)} depth={0} {...props} />
      </nav>
    </details>
  );
}
