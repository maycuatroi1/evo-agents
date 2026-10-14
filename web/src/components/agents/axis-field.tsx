"use client";

import { Plus, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useId, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { type Axis } from "@/lib/agents";
import { MAX_ALLOW, normalizedItem, validItem } from "@/lib/agent-rules";
import { cn } from "@/lib/utils";

import { AXIS_ICONS } from "./axis";
import { FieldError } from "./form-parts";
import { type AxisDraft, withItem, withoutItem } from "./model";
import type { AxisOption } from "./options";

/** A list longer than this gets a filter field above it. */
const FILTER_FROM = 8;

/** The words an option shows: a tool, a runtime or the App's token in the visitor's language, anything else as named. */
function useOptionText(axis: Axis) {
  const t = useTranslations("agents.form.axis");
  const tTools = useTranslations("agents.tools");
  const tRuntime = useTranslations("runs.runtime");
  return (option: AxisOption): { title: string; detail: string | null; mono: boolean } => {
    if (axis === "tool" && option.tag && option.tag in TOOL_KEYS) return { title: option.value, detail: tTools(option.tag as ToolKey), mono: true };
    if (axis === "runtime" && option.tag && option.tag in RUNTIME_KEYS) return { title: tRuntime(option.tag as RuntimeKey), detail: option.value, mono: false };
    if (option.tag === "githubApp") return { title: option.value, detail: t("githubApp"), mono: true };
    if (option.tag === "label") return { title: option.value, detail: t("labelWorkers", { count: Number(option.detail ?? 0) }), mono: true };
    if (option.tag === "custom") return { title: option.value, detail: t("custom"), mono: true };
    if (option.tag === "global" || option.tag === "project") {
      return { title: option.value, detail: [t(`skillScope.${option.tag}`), option.detail].filter(Boolean).join(": "), mono: true };
    }
    return { title: option.value, detail: option.detail ?? null, mono: true };
  };
}

const TOOL_KEYS = { read: true, edit: true, bash: true, web: true, agent: true } as const;
type ToolKey = keyof typeof TOOL_KEYS;
const RUNTIME_KEYS = { "claude-code": true, opencode: true, codex: true } as const;
type RuntimeKey = keyof typeof RUNTIME_KEYS;

/**
 * One axis of the agent's permissions in its form: every value, or only those picked. Picking lists what the hub has
 * for the axis (the options), with a filter once the list is long, and takes a name typed by hand for what the admin
 * cannot see (another member's secret or worker, an MCP server); a name of the wrong shape is refused where it is
 * typed. An allowlist left empty allows nothing on the axis, and says so.
 */
export function AxisField({
  axis,
  value,
  onChange,
  options,
  loading,
  failed,
  problem,
  disabled = false,
}: {
  axis: Axis;
  value: AxisDraft;
  onChange: (value: AxisDraft) => void;
  options: AxisOption[];
  loading: boolean;
  failed: boolean;
  problem: "tooMany" | "item" | undefined;
  disabled?: boolean;
}) {
  const t = useTranslations("agents.form.axis");
  const tAxis = useTranslations("agents.axes");
  const optionText = useOptionText(axis);
  const ids = useId();
  const [filter, setFilter] = useState("");
  const [typed, setTyped] = useState("");
  const [typedProblem, setTypedProblem] = useState(false);
  const Icon = AXIS_ICONS[axis];
  const title = tAxis(`${axis}.title`);
  const noun = tAxis(`${axis}.noun`);

  const known = new Set(options.map((option) => option.value));
  const listed: AxisOption[] = [...options, ...value.allow.filter((item) => !known.has(item)).map((item) => ({ value: item, tag: "custom" }))];
  const needle = filter.trim().toLowerCase();
  const shown = needle ? listed.filter((option) => option.value.toLowerCase().includes(needle) || (option.detail ?? "").toLowerCase().includes(needle)) : listed;

  const add = () => {
    const item = normalizedItem(axis, typed);
    if (!item) return;
    if (!validItem(axis, item)) {
      setTypedProblem(true);
      return;
    }
    onChange(withItem(value, axis, item));
    setTyped("");
    setTypedProblem(false);
  };
  const onTypedKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault(); // adds the name instead of sending the form
      add();
    }
  };

  return (
    <div className="rounded-md border px-3 py-3 sm:px-4" data-testid={`agent-axis-${axis}`} data-mode={value.mode}>
      <fieldset className="flex min-w-0 flex-col gap-2.5" aria-describedby={`${ids}-hint`}>
        <legend className="sr-only">{title}</legend>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <span className="flex items-center gap-2 text-sm font-medium" aria-hidden="true">
            <Icon className="size-4 text-muted-foreground" />
            {title}
          </span>
          <Segmented
            label={t("mode", { axis: noun })}
            value={value.mode}
            onChange={(mode) => onChange({ ...value, mode })}
            options={[
              { value: "all", label: t("all"), testId: `agent-axis-${axis}-all` },
              { value: "allowlist", label: t("allowlist"), testId: `agent-axis-${axis}-allowlist` },
            ]}
          />
        </div>
        <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground">
          {tAxis(`${axis}.hint`)}
        </p>
        {value.mode === "all" ? null : (
          <>
            {failed ? <p className="text-xs text-attention">{t("failed", { axis: noun })}</p> : null}
            {listed.length > FILTER_FROM ? (
              <Input
                type="search"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder={t("filter", { axis: noun })}
                aria-label={t("filter", { axis: noun })}
                className="h-8"
                data-testid={`agent-axis-${axis}-filter`}
              />
            ) : null}
            {loading ? (
              <Skeleton className="h-20 w-full" />
            ) : listed.length === 0 ? (
              <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">{t("noOptions")}</p>
            ) : (
              <ul className="flex max-h-56 flex-col overflow-y-auto rounded-md border" aria-label={t("choices", { axis: title })} data-testid={`agent-axis-${axis}-options`}>
                {shown.map((option) => {
                  const text = optionText(option);
                  const checked = value.allow.includes(option.value);
                  return (
                    <li key={option.value} className="border-t first:border-t-0">
                      <label className={cn("flex min-h-9 cursor-pointer items-center gap-2.5 px-3 py-1.5 transition-colors hover:bg-muted/50 max-md:min-h-11", checked && "bg-surface-selected")}>
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={disabled}
                          onChange={(event) => onChange(event.target.checked ? withItem(value, axis, option.value) : withoutItem(value, option.value))}
                          className="size-4 shrink-0 cursor-pointer accent-primary"
                          data-testid={`agent-axis-${axis}-option`}
                          data-value={option.value}
                        />
                        <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
                          <span className={cn("text-sm [overflow-wrap:anywhere]", text.mono && "font-mono text-[13px]")}>{text.title}</span>
                          {text.detail ? <span className="text-xs text-fg-subtle [overflow-wrap:anywhere]">{text.detail}</span> : null}
                        </span>
                      </label>
                    </li>
                  );
                })}
                {shown.length === 0 ? <li className="px-3 py-2 text-xs text-muted-foreground">{t("noMatch")}</li> : null}
              </ul>
            )}
            <div className="flex min-w-0 items-start gap-2">
              <label htmlFor={`${ids}-typed`} className="sr-only">
                {t("typedLabel", { axis: noun })}
              </label>
              <Input
                id={`${ids}-typed`}
                value={typed}
                onChange={(event) => {
                  setTyped(event.target.value);
                  setTypedProblem(false);
                }}
                onKeyDown={onTypedKey}
                placeholder={t(`typed.${axis}`)}
                autoComplete="off"
                spellCheck={false}
                disabled={disabled}
                aria-invalid={typedProblem || undefined}
                aria-describedby={typedProblem ? `${ids}-typed-error` : undefined}
                className="h-8 min-w-0 flex-1 font-mono placeholder:font-sans"
                data-testid={`agent-axis-${axis}-typed`}
              />
              <Button type="button" variant="outline" size="sm" className="h-8 shrink-0" onClick={add} disabled={disabled || !typed.trim()} data-testid={`agent-axis-${axis}-add`}>
                <Plus aria-hidden="true" />
                {t("add")}
              </Button>
            </div>
            {typedProblem ? <FieldError id={`${ids}-typed-error`} text={t(`invalid.${axis}`)} /> : null}
            <p
              className={cn("flex items-start gap-1.5 text-xs", value.allow.length === 0 ? "font-medium text-attention" : "text-fg-subtle")}
              aria-live="polite"
              data-testid={`agent-axis-${axis}-count`}
            >
              {value.allow.length === 0 ? <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" /> : null}
              {value.allow.length === 0 ? t("nothing", { axis: noun }) : t("count", { count: value.allow.length, max: MAX_ALLOW })}
            </p>
            {problem ? <FieldError text={problem === "tooMany" ? t("tooMany", { max: MAX_ALLOW }) : t("badItem")} /> : null}
          </>
        )}
      </fieldset>
    </div>
  );
}
