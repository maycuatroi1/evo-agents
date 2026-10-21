"use client";

import { ChevronDown, Layers, type LucideIcon, MessagesSquare, Settings2, SquareTerminal, Wrench } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

import { LOG_GROUPS, type LogGroup } from "./log-model";

/** Each group of the Raw log's lines, with its icon (run-log.tsx). */
export const GROUP_ICON: Record<LogGroup, LucideIcon> = {
  agent: MessagesSquare,
  tools: Wrench,
  output: SquareTerminal,
  system: Settings2,
};

/**
 * The log's groups on a phone: one 44 px button with the group shown (named "Show: Tools"), opening a menu of the
 * groups with their line counts, instead of a row of chips that would wrap over three lines. Under the search field,
 * beside Follow and Pause.
 */
export function GroupMenu({
  group,
  counts,
  total,
  onSelect,
}: {
  group: LogGroup | null;
  counts: Record<LogGroup, number>;
  total: number;
  onSelect: (group: LogGroup | null) => void;
}) {
  const t = useTranslations("runs.detail.log");
  const format = useFormatter();
  const options: { value: LogGroup | null; label: string; icon: LucideIcon; count: number }[] = [
    { value: null, label: t("groups.all"), icon: Layers, count: total },
    ...LOG_GROUPS.map((value) => ({ value, label: t(`groups.${value}`), icon: GROUP_ICON[value], count: counts[value] })),
  ];
  const shown = options.find((option) => option.value === group) ?? options[0];
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn("min-w-0 shrink-0", group !== null && "border-brand bg-surface-selected text-foreground")}
          // "Show: Tools" to a screen reader; the group's name and icon on screen.
          aria-label={t("groupMenu", { group: shown.label })}
          data-testid="log-group-menu"
          data-group={group ?? "all"}
        >
          <shown.icon aria-hidden="true" />
          {shown.label}
          <ChevronDown aria-hidden="true" className="text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuLabel>{t("filter")}</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={group ?? ""}
          onValueChange={(value) => onSelect((LOG_GROUPS as readonly string[]).includes(value) ? (value as LogGroup) : null)}
        >
          {options.map((option) => (
            <DropdownMenuRadioItem key={option.value ?? ""} value={option.value ?? ""} data-testid={`log-group-${option.value ?? "all"}`}>
              <option.icon aria-hidden="true" />
              {option.label}
              <span className="ml-auto pl-3 text-xs text-fg-subtle tabular-nums">
                <span aria-hidden="true">{format.number(option.count)}</span>
                <span className="sr-only">{t("lines", { count: option.count })}</span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
