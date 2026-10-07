"use client";

import { Eye, PenLine, ShieldCheck, UserX } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";

const ROLE_ICON = { admin: ShieldCheck, writer: PenLine, reader: Eye, none: UserX } as const;

/** A project role as an icon and a word in a tag: a role names what someone may do, so it is not a round state pill. */
export function RoleBadge({ role }: { role: string | null }) {
  const t = useTranslations("roles");
  const key = roleLabelKey(role);
  const Icon = ROLE_ICON[key];
  return (
    <Tag data-role={key}>
      <Icon aria-hidden="true" />
      {t(key)}
    </Tag>
  );
}

export function roleLabelKey(role: string | null): "admin" | "writer" | "reader" | "none" {
  return role === "admin" || role === "writer" || role === "reader" ? role : "none";
}
