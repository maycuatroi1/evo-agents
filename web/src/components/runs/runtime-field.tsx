"use client";

import { ScrollText, SquareTerminal } from "lucide-react";
import { useTranslations } from "next-intl";

import { Section, Tile } from "./dispatch-fields";
import { MODES, type RequestedRuntime, type RunMode, RUNTIMES } from "./queries";
import { RuntimeLogo } from "./runtime-logo";

/**
 * The runtime of a dispatch, as tiles with each runtime's mark: any (the worker picks), Claude Code, opencode and Codex
 * CLI. `reach` counts, by runtime, the visitor's workers for the project that report it, once they are read: a tile
 * says so only when none does. What each one runs shows from the info icon.
 */
export function RuntimeField({
  value,
  onChange,
  reach,
}: {
  value: RequestedRuntime;
  onChange: (runtime: RequestedRuntime) => void;
  reach: Partial<Record<RequestedRuntime, number>> | null;
}) {
  const t = useTranslations("runs.dispatch");
  const tRuntime = useTranslations("runs.runtime");
  return (
    <Section legend={t("runtime")} info={t("runtimeInfo")} testId="dispatch-runtime">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {(["any", ...RUNTIMES] as const).map((runtime) => (
          <Tile
            key={runtime}
            name="runtime"
            value={runtime}
            checked={value === runtime}
            onChange={() => onChange(runtime)}
            icon={<RuntimeLogo runtime={runtime} />}
            title={runtime === "any" ? t("runtimeAny") : tRuntime(runtime)}
            note={reach && reach[runtime] === 0 ? t("runtimeNone") : undefined}
            testId={`dispatch-runtime-${runtime}`}
          />
        ))}
      </div>
    </Section>
  );
}

const MODE_ICONS = { headless: ScrollText, interactive: SquareTerminal } as const;

/** Headless or interactive from the start, as two tiles; what each one means shows from the info icon. */
export function ModeField({ value, onChange }: { value: RunMode; onChange: (mode: RunMode) => void }) {
  const t = useTranslations("runs.dispatch");
  return (
    <Section legend={t("mode")} info={t("modeInfo")} testId="dispatch-mode">
      <div className="grid grid-cols-2 gap-2">
        {MODES.map((mode) => {
          const Icon = MODE_ICONS[mode];
          return (
            <Tile
              key={mode}
              name="mode"
              value={mode}
              checked={value === mode}
              onChange={() => onChange(mode)}
              icon={<Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />}
              title={t(`modes.${mode}`)}
              testId={`dispatch-mode-${mode}`}
            />
          );
        })}
      </div>
    </Section>
  );
}
