"use client";

import { Activity, Ban, CircleCheck, CircleDot, Clock, FolderPlus, Globe, Hourglass, Laptop, MonitorCheck, Server, ShieldCheck, UserPlus } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";

import type { AdminToken } from "./data";
import type { InviteRow } from "./invite-data";

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
  const Icon = kind === "machine" ? Laptop : kind === "worker" ? Server : kind === "run" ? Activity : Globe;
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

export function InviteKindBadge({ kind }: { kind: InviteRow["kind"] }) {
  const t = useTranslations("admin.invites.kinds");
  const Icon = kind === "join" ? UserPlus : FolderPlus;
  return (
    <Tag data-kind={kind}>
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

const INVITE_STATUS = {
  open: { variant: "success", icon: CircleDot },
  accepted: { variant: "outline", icon: CircleCheck },
  revoked: { variant: "outline", icon: Ban },
  expired: { variant: "warning", icon: Hourglass },
} as const;

export function InviteStatusBadge({ status }: { status: InviteRow["status"] }) {
  const t = useTranslations("admin.invites.statuses");
  const { variant, icon: Icon } = INVITE_STATUS[status];
  return (
    <Badge variant={variant} data-state={status} data-testid="invite-status">
      <Icon aria-hidden="true" />
      {t(status)}
    </Badge>
  );
}
