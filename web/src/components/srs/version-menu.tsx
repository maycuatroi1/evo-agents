"use client";

import { Check, ChevronDown } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";

import type { SrsVersion } from "./api";
import { VersionBadge } from "./badges";

/**
 * Which version a page shows, as a menu of links (one per version, the newest first, each with its state and author),
 * so a version opens like any page and Back returns; the menu is not modal, as the hub's other menus.
 */
export function VersionMenu({
  versions,
  shown,
  hrefOf,
  testId = "srs-version-menu",
}: {
  versions: SrsVersion[];
  shown: number;
  hrefOf: (version: number) => Route;
  testId?: string;
}) {
  const t = useTranslations("srs.versionMenu");
  const tState = useTranslations("srs.versionState");
  const current = versions.find((version) => version.number === shown);
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="secondary" size="sm" data-testid={testId}>
          {t("showing", { version: shown, state: current ? tState(current.state) : "" })}
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-auto min-w-64">
        {versions.map((version) => (
          <DropdownMenuItem key={version.number} asChild>
            <Link href={hrefOf(version.number)} aria-current={version.number === shown ? "true" : undefined} data-version={version.number}>
              <span className="w-4">{version.number === shown ? <Check aria-hidden="true" /> : null}</span>
              <span className="font-mono text-xs">v{version.number}</span>
              <VersionBadge state={version.state} />
              <span className="ml-auto pl-3 text-xs text-fg-subtle">{version.author}</span>
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
