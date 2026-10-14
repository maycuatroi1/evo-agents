"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { type Agent, allowed, AXES, type Axis } from "@/lib/agents";
import { cn } from "@/lib/utils";

import { AXIS_ICONS } from "./axis";

const RUNTIME_KEYS = { "claude-code": true, opencode: true, codex: true } as const;
type RuntimeKey = keyof typeof RUNTIME_KEYS;

/** One allowlist item: a runtime by its name, anything else in mono as the allowlist names it. */
function Item({ axis, value }: { axis: Axis; value: string }) {
  const tRuntime = useTranslations("runs.runtime");
  const text = axis === "runtime" && value in RUNTIME_KEYS ? tRuntime(value as RuntimeKey) : value;
  return (
    <li
      className={cn("inline-flex h-[22px] max-w-full items-center rounded-xs bg-surface-sunken px-1.5 text-xs text-foreground", text === value && "font-mono")}
      data-value={value}
    >
      <span className="truncate">{text}</span>
    </li>
  );
}

/**
 * The seven axes of an agent's permissions, read only: each axis with its icon, its name and what it governs, then
 * "Every ..." or the values its allowlist names; an empty allowlist says nothing is allowed, in the attention tone,
 * since a run under the agent then cannot use that axis at all.
 */
export function AgentPermissions({ agent }: { agent: Agent }) {
  const t = useTranslations("agents.detail.permissions");
  const tAxis = useTranslations("agents.axes");
  return (
    <Card data-testid="agent-permissions">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{agent.is_default ? t("defaultHint") : t("hint")}</CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <dl className="flex flex-col">
          {AXES.map((axis) => {
            const Icon = AXIS_ICONS[axis];
            const allow = allowed(agent.permissions, axis);
            return (
              <div
                key={axis}
                className="grid gap-x-6 gap-y-1.5 border-t px-4 py-3 md:grid-cols-[minmax(12rem,1fr)_minmax(0,2fr)]"
                data-testid={`agent-permission-${axis}`}
                data-mode={allow === null ? "all" : "allowlist"}
              >
                <dt className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex items-center gap-2 text-sm font-medium">
                    <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
                    {tAxis(`${axis}.title`)}
                  </span>
                  <span className="text-xs text-pretty text-fg-subtle">{tAxis(`${axis}.hint`)}</span>
                </dt>
                <dd className="flex min-w-0 items-start">
                  {allow === null ? (
                    <span className="text-sm text-muted-foreground">{tAxis(`${axis}.all`)}</span>
                  ) : allow.length === 0 ? (
                    <span className="flex items-start gap-1.5 text-sm font-medium text-attention">
                      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                      {t("nothing")}
                    </span>
                  ) : (
                    <ul className="flex min-w-0 flex-wrap gap-1.5" aria-label={t("allowed", { axis: tAxis(`${axis}.title`) })}>
                      {allow.map((value) => (
                        <Item key={value} axis={axis} value={value} />
                      ))}
                    </ul>
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
      </CardContent>
    </Card>
  );
}
