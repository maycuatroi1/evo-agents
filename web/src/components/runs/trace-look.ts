import { ArrowLeftRight, Brain, FilePen, FileText, FolderInput, Globe, type LucideIcon, Search, Terminal, Trash2, Wrench } from "lucide-react";
import { useTranslations } from "next-intl";

import { durationParts, type ToolKind } from "./trace-model";

/**
 * What the run's Trace and the phone's decision screen (its "What the agent did so far") share: the icon of each kind of
 * tool call, and how a duration reads.
 */
export const KIND_ICON: Record<ToolKind, LucideIcon> = {
  read: FileText,
  edit: FilePen,
  delete: Trash2,
  move: FolderInput,
  search: Search,
  execute: Terminal,
  think: Brain,
  fetch: Globe,
  switch_mode: ArrowLeftRight,
  other: Wrench,
};

/** A trace's durations: "under 0.1s", "4.2s", "8m 12s", "1h 5m". */
export function useTraceDuration() {
  const t = useTranslations("runs.detail.trace.duration");
  return (ms: number) => {
    const { hours, minutes, seconds, tenths } = durationParts(ms);
    if (ms < 100) return t("under");
    if (hours > 0) return t("hours", { hours, minutes });
    if (minutes > 0) return t("minutes", { minutes, seconds });
    return t("seconds", { seconds, tenths });
  };
}
