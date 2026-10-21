"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, type Ref, useId } from "react";

import { cn } from "@/lib/utils";

import { AgentPickTag } from "./badges";
import type { Decision, DecisionOption } from "./queries";

export type HeadingLevel = 2 | 3;

/** The DecisionCard's question, its heading: quiet once the decision is closed. */
export function Question({
  level,
  id,
  headingRef,
  quiet,
  children,
}: {
  level: HeadingLevel;
  id: string;
  headingRef?: Ref<HTMLHeadingElement>;
  quiet: boolean;
  children: ReactNode;
}) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <Tag
      id={id}
      ref={headingRef}
      tabIndex={headingRef ? -1 : undefined}
      className={cn(
        "rounded-xs whitespace-pre-line text-pretty outline-none [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
        quiet ? "text-[13px] leading-[19px] font-normal text-muted-foreground" : "text-[15px] leading-[22px] font-medium text-foreground",
      )}
    >
      {children}
    </Tag>
  );
}


/**
 * An option as the kit's radio card: the label with the agent's pick tagged, then what it does next. On the phone's
 * screen (`screen`) it has 14 px of padding, a 15 px title and a 14 px description.
 */
export function OptionCard({
  option,
  name,
  checked,
  onPick,
  screen = false,
}: {
  option: DecisionOption;
  name: string;
  checked: boolean;
  onPick: () => void;
  screen?: boolean;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "grid cursor-pointer grid-cols-[16px_minmax(0,1fr)] gap-3 rounded-sm border border-border-strong bg-card transition-colors hover:bg-accent max-md:min-h-11",
        "has-checked:border-brand has-checked:bg-surface-selected",
        screen ? "p-3.5" : "p-3",
      )}
      data-testid={`decision-choice-${option.key}`}
      data-key={option.key}
      data-checked={checked || undefined}
    >
      <input
        id={id}
        type="radio"
        name={name}
        value={option.key}
        checked={checked}
        onChange={onPick}
        className="mt-0.5 size-4 cursor-pointer accent-brand"
        aria-labelledby={`${id}-title`}
        aria-describedby={option.description ? `${id}-description` : undefined}
      />
      <OptionText option={option} id={id} screen={screen} />
    </label>
  );
}

function OptionText({ option, id, screen = false }: { option: DecisionOption; id: string; screen?: boolean }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span
        id={`${id}-title`}
        className={cn("flex flex-wrap items-center gap-2 font-medium text-foreground [overflow-wrap:anywhere]", screen ? "text-[15px] leading-[22px]" : "text-sm")}
      >
        {option.label}
        {/* A space for the accessible name ("Deploy now Agent's pick"); a flex container does not draw it. */}
        {option.recommended ? <> <AgentPickTag /></> : null}
      </span>
      {option.description ? (
        <span
          id={`${id}-description`}
          className={cn("whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]", screen ? "text-sm leading-5" : "text-[13px] leading-[18px]")}
        >
          {option.description}
        </span>
      ) : null}
    </span>
  );
}

/** The options as they were offered, for anyone who cannot answer, the agent's pick tagged. */
export function OptionList({ decision, screen = false }: { decision: Decision; screen?: boolean }) {
  const t = useTranslations("inbox.answer");
  const ids = useId();
  return (
    <div className="flex flex-col gap-2" data-testid="decision-options">
      <p id={`${ids}-legend`} className="sr-only">
        {t("legend")}
      </p>
      <ul className="flex flex-col gap-2" aria-labelledby={`${ids}-legend`}>
        {decision.options.map((option, index) => (
          <li key={option.key} className={cn("rounded-sm border bg-card", screen ? "p-3.5" : "p-3")} data-testid="decision-option" data-key={option.key}>
            <OptionText option={option} id={`${ids}-${index}`} screen={screen} />
          </li>
        ))}
      </ul>
    </div>
  );
}
