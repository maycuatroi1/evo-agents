"use client";

import { Network } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { NAME_LINK, Tag } from "@/components/data/identifier";
import { observeHref, type WorkCard } from "@/lib/work";

import type { ChatHeadSlots } from "./chat-header";
import { WorkStatusPill } from "./work-look";

/**
 * A work chat's head (web/DESIGN.md, Work card): the work's title, beside it the work's state and Work #N (its trace), and over the
 * members its system, linking to Observe of the system, and its coordinator, the system's entry agent.
 */
export function workHeadSlots(card: WorkCard | null): ChatHeadSlots {
  if (!card) return {};
  return {
    heading: card.title,
    tags: (
      <>
        <WorkStatusPill status={card.status} size="lg" testId="work-head-status" />
        <WorkNumber id={card.trace_id} />
      </>
    ),
    sub: <WorkSystemLine card={card} />,
  };
}

function WorkNumber({ id }: { id: number }) {
  const t = useTranslations("chats.work.head");
  return (
    <Tag className="tabular-nums" data-testid="work-head-number">
      {t("number", { id })}
    </Tag>
  );
}

function WorkSystemLine({ card }: { card: WorkCard }) {
  const t = useTranslations("chats.work.head");
  const name = card.system.name ?? card.system.slug;
  const href = observeHref(card.system);
  return (
    <span className="inline-flex max-w-full flex-wrap items-center gap-x-3 gap-y-1" data-testid="work-head-sub">
      <span className="inline-flex min-w-0 items-center gap-1">
        <Network className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="sr-only">{t("system")}</span>
        {href ? (
          <Link href={href} className={`${NAME_LINK} truncate`} title={t("observe", { name })} data-testid="work-head-system">
            {name}
          </Link>
        ) : (
          <span className="truncate" data-testid="work-head-system">
            {name}
          </span>
        )}
      </span>
      {card.coordinator ? (
        <span className="inline-flex min-w-0 items-center gap-1" data-testid="work-head-coordinator">
          {t("coordinator")}
          <span className="font-mono text-foreground">@{card.coordinator}</span>
        </span>
      ) : null}
    </span>
  );
}
