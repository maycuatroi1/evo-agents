"use client";

import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Button } from "@/components/ui/button";
import type { TierSetting } from "@/lib/notification-queries";
import { cn } from "@/lib/utils";

import { choiceOf, DEFAULT_CHOICE, defaultTier, isDefaultInterrupt, tierBody, type TierChoice, tierGroups, TIERS } from "./model";
import { SettingsCard } from "./parts";
import { useSettingsWrite } from "./use-settings-write";

/** Questions, proposals and Today have words of their own; every notice's kind is named as the Inbox names it. */
const OWN_WORDS = ["decision", "proposal", "today"];

/** A kind of notification in words, and the words of a kind a later hub adds that this web does not know. */
export function useKindName() {
  const t = useTranslations("notifications.tiers.kind");
  const notice = useTranslations("inbox.noticeKind");
  return (kind: string) => (OWN_WORDS.includes(kind) ? t(kind as never) : notice.has(kind as never) ? notice(kind as never) : kind);
}

function TierRow({ setting, write }: { setting: TierSetting; write: ReturnType<typeof useTierWrite> }) {
  const t = useTranslations("notifications.tiers");
  const id = useId();
  const kindName = useKindName();
  const name = kindName(setting.kind);
  const pending = write.pending?.kind === setting.kind ? write.pending.choice : null;
  const value = pending ?? choiceOf(setting);
  const hint = isDefaultInterrupt(setting.kind) || setting.kind === "today" ? t(`hint.${setting.kind as "decision"}`) : null;
  return (
    <li className="flex min-w-0 flex-col gap-2 border-t px-4 py-3 first:border-t-0 sm:flex-row sm:items-center sm:gap-4" data-testid="tier" data-kind={setting.kind} data-tier={setting.tier} data-chosen={setting.chosen ? "true" : "false"}>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <label htmlFor={id} className="text-sm leading-5 font-medium text-pretty text-foreground">
          {name}
        </label>
        {hint ? (
          <p id={`${id}-hint`} className="text-xs leading-4 text-pretty text-fg-subtle">
            {hint}
          </p>
        ) : null}
      </div>
      <NativeSelect
        id={id}
        value={value}
        onChange={(event) => write.write({ kind: setting.kind, choice: event.target.value as TierChoice })}
        aria-busy={pending !== null || undefined}
        aria-describedby={hint ? `${id}-hint` : undefined}
        className="w-full sm:w-52 sm:shrink-0"
        data-testid="tier-select"
      >
        <NativeSelectOption value={DEFAULT_CHOICE}>{t("choice.default", { tier: t(`choice.${defaultTier(setting.kind)}`) })}</NativeSelectOption>
        {TIERS.map((tier) => (
          <NativeSelectOption key={tier} value={tier}>
            {t(`choice.${tier}`)}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </li>
  );
}

function useTierWrite() {
  const t = useTranslations("notifications.tiers");
  const kindName = useKindName();
  return useSettingsWrite<{ kind: TierSetting["kind"]; choice: TierChoice }>({
    body: ({ kind, choice }) => tierBody(kind, choice),
    saved: ({ kind, choice }) =>
      t("saved", { kind: kindName(kind), tier: choice === DEFAULT_CHOICE ? t("choice.default", { tier: t(`choice.${defaultTier(kind)}`) }) : t(`choice.${choice}`) }),
    failed: ({ kind }) => t("failed", { kind: kindName(kind) }),
  });
}

/**
 * When each kind of notification pushes (docs/notifications.md, Tiers): Push now, Today and Inbox, or Inbox only, or
 * the hub's default, which pushes a question asked of the member, their own failed run, a release gone wrong and a
 * stopped merge queue. A change applies at once. The kinds the page shows first are those the hub pushes by default
 * and the few a member weighs most; the rest fold under them, open when one of them was chosen.
 */
export function TiersCard({ tiers, className }: { tiers: TierSetting[]; className?: string }) {
  const t = useTranslations("notifications.tiers");
  const ids = useId();
  const write = useTierWrite();
  const { first, rest } = tierGroups(tiers);
  const [open, setOpen] = useState(() => rest.some((setting) => setting.chosen));
  return (
    <SettingsCard title={t("title")} testId="tiers-card" className={className}>
      <p className="border-b px-4 py-3 text-[13px] leading-[18px] text-pretty text-muted-foreground">{t("description")}</p>
      <ul aria-label={t("title")} data-testid="tiers-first">
        {first.map((setting) => (
          <TierRow key={setting.kind} setting={setting} write={write} />
        ))}
      </ul>
      {rest.length > 0 ? (
        <>
          <div className="border-t px-2 py-1.5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-expanded={open}
              aria-controls={`${ids}-rest`}
              onClick={() => setOpen(!open)}
              data-testid="tiers-more"
            >
              <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
              {open ? t("fewer") : t("more", { count: rest.length })}
            </Button>
          </div>
          <ul id={`${ids}-rest`} hidden={!open} aria-label={t("restLabel")} className="border-t" data-testid="tiers-rest">
            {rest.map((setting) => (
              <TierRow key={setting.kind} setting={setting} write={write} />
            ))}
          </ul>
        </>
      ) : null}
    </SettingsCard>
  );
}
