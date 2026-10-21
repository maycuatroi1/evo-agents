"use client";

import { BrainCircuit, Container, FolderKanban, KeyRound, type LucideIcon, Server, Sparkles, SquareTerminal, Wrench } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import type { Axis } from "@/lib/agents";

/** One icon per axis (web/DESIGN.md, Iconography): the concept's own where the hub has one (Skills, Workers, a project). */
export const AXIS_ICONS: Record<Axis, LucideIcon> = {
  credential: KeyRound,
  tool: Wrench,
  skill: Sparkles,
  worker: Server,
  project: FolderKanban,
  model: BrainCircuit,
  runtime: SquareTerminal,
  environment: Container,
};

/** Tags a row shows before the rest fold into "+N", so a row of the list stays one line high. */
const SHOWN_TAGS = 3;

/**
 * The axes an agent limits, each as a tag (its icon and its name), on one line: the first SHOWN_TAGS, then "+N" whose
 * tooltip and screen-reader text name the rest; "Every permission" when it limits none.
 */
export function AxisTags({ axes, testId = "agent-limits" }: { axes: readonly string[]; testId?: string }) {
  const t = useTranslations("agents.axes");
  const tList = useTranslations("agents.list");
  const known = axes.filter((axis): axis is Axis => axis in AXIS_ICONS);
  const folded = known.slice(SHOWN_TAGS).map((axis) => t(`${axis}.title`));
  if (known.length === 0) {
    return (
      <span className="text-[13px] text-fg-subtle" data-testid={testId} data-limits="">
        {tList("everyPermission")}
      </span>
    );
  }
  return (
    <span className="flex flex-nowrap items-center gap-1" data-testid={testId} data-limits={known.join(" ")}>
      <span className="sr-only">{tList("limitsLabel")}</span>
      {known.slice(0, SHOWN_TAGS).map((axis) => {
        const Icon = AXIS_ICONS[axis];
        return (
          <Tag key={axis}>
            <Icon aria-hidden="true" />
            {t(`${axis}.title`)}
          </Tag>
        );
      })}
      {folded.length ? (
        <Tag title={folded.join(", ")}>
          <span aria-hidden="true">{tList("more", { count: folded.length })}</span>
          <span className="sr-only">{folded.join(", ")}</span>
        </Tag>
      ) : null}
    </span>
  );
}
