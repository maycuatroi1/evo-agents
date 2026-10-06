"use client";

import { Ban, CircleCheck, Clock, Globe, Hourglass, Laptop, MonitorCheck, Server, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";

import type { AdminToken } from "./data";

/**
 * Badges of the admin area: an icon and a word each, so colour is never the only signal. A role or a kind (Hub admin,
 * a token's kind, this session) is a tag with square-ish corners; a state (active, expired, revoked) is a pill.
 */

export function HubAdminBadge() {
  const t = useTranslations("admin.members");
  return (
    <Tag data-testid="badge-hub-admin">
      <ShieldCheck aria-hidden="true" />
      {t("hubAdmin")}
    </Tag>
  );
}

export function NotSignedInBadge() {
  const t = useTranslations("admin.members");
  return (
    <Badge variant="outline" data-testid="badge-not-signed-in">
      <Clock aria-hidden="true" />
      {t("notSignedIn")}
    </Badge>
  );
}

export function TokenKindBadge({ kind }: { kind: AdminToken["kind"] }) {
  const t = useTranslations("admin.tokens.kinds");
  const Icon = kind === "machine" ? Laptop : kind === "worker" ? Server : Globe;
  return (
    <Tag>
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

export function TokenStateBadge({ state }: { state: AdminToken["state"] }) {
  const t = useTranslations("admin.tokens.states");
  if (state === "active")
    return (
      <Badge variant="success" data-state="active">
        <CircleCheck aria-hidden="true" />
        {t("active")}
      </Badge>
    );
  if (state === "expired")
    return (
      <Badge variant="warning" data-state="expired">
        <Hourglass aria-hidden="true" />
        {t("expired")}
      </Badge>
    );
  return (
    <Badge variant="outline" data-state="revoked">
      <Ban aria-hidden="true" />
      {t("revoked")}
    </Badge>
  );
}

export function CurrentSessionBadge() {
  const t = useTranslations("admin.tokens");
  return (
    <Tag className="bg-brand-soft text-brand">
      <MonitorCheck aria-hidden="true" />
      {t("current")}
    </Tag>
  );
}
