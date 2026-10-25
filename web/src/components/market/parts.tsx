"use client";

import {
  ArrowUpCircle,
  Ban,
  CircleCheck,
  CircleDashed,
  Cpu,
  KeyRound,
  PackageCheck,
  PackageX,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { useMoney } from "@/components/data/facts";
import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";

import type { MarketPlugin, MarketStatus } from "./queries";

/**
 * The small parts the market pages share (web/DESIGN.md, Plugins): where a plugin stands on this hub as a pill, its
 * price and tier as tags, and the publisher with what signs its releases. Colour is for the state alone.
 */

export const MARKET_LOOKS = {
  not_installed: { tone: "neutral", icon: CircleDashed },
  installed: { tone: "success", icon: CircleCheck },
  upgrade: { tone: "review", icon: ArrowUpCircle },
  in_image: { tone: "success", icon: PackageCheck },
  not_in_image: { tone: "outline", icon: PackageX },
  needs_license: { tone: "outline", icon: KeyRound },
  unavailable: { tone: "attention", icon: TriangleAlert },
} as const satisfies Record<MarketStatus, StatusLook>;

export function MarketStatusBadge({ status, ...props }: StatusPillProps & { status: MarketStatus }) {
  const t = useTranslations("market.status");
  return <StatusPill look={MARKET_LOOKS[status]} label={t(status)} data-status={status} data-testid="market-status" {...props} />;
}

export function PriceTag({ price }: { price: MarketPlugin["price"] }) {
  const t = useTranslations("market");
  const money = useMoney();
  if (price.kind === "free") return <Tag data-testid="market-price">{t("price.free")}</Tag>;
  const amount = price.usd_per_hub_month;
  return (
    <Tag data-testid="market-price">
      <KeyRound aria-hidden="true" />
      {amount === null || amount === undefined ? t("price.subscription") : t("price.perMonth", { amount: money(amount) })}
    </Tag>
  );
}

export function TierTag({ tier }: { tier: MarketPlugin["tier"] }) {
  const t = useTranslations("market.tier");
  return (
    <Tag>
      {tier === "core" ? <Cpu aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />}
      {t(tier)}
    </Tag>
  );
}

/** Why nothing can be installed from here, for a plugin that is not a free isolated one or whose release fails. */
export function NoInstallReason({ plugin }: { plugin: MarketPlugin }) {
  const t = useTranslations("market.reason");
  const reason =
    plugin.tier === "core"
      ? plugin.status === "in_image"
        ? t("inImage")
        : t("core")
      : plugin.price.kind !== "free"
        ? t("paid")
        : plugin.status === "unavailable"
          ? t("unavailable")
          : null;
  if (reason === null) return null;
  return (
    <p className="flex items-start gap-2 text-sm text-muted-foreground" data-testid="market-reason">
      {plugin.status === "unavailable" ? (
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-attention" aria-hidden="true" />
      ) : (
        <Ban className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      )}
      <span className="min-w-0 [overflow-wrap:anywhere]">{reason}</span>
    </p>
  );
}
