"use client";

import { Eye, PenLine, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";

/** A project role as text and icon, so the colour is never the only signal. */
export function RoleBadge({ role }: { role: string | null }) {
  const t = useTranslations("roles");
  if (role === "admin")
    return (
      <Badge variant="warning" data-role="admin">
        <ShieldCheck aria-hidden="true" />
        {t("admin")}
      </Badge>
    );
  if (role === "writer")
    return (
      <Badge variant="info" data-role="writer">
        <PenLine aria-hidden="true" />
        {t("writer")}
      </Badge>
    );
  if (role === "reader")
    return (
      <Badge variant="secondary" data-role="reader">
        <Eye aria-hidden="true" />
        {t("reader")}
      </Badge>
    );
  return (
    <Badge variant="outline" data-role="none">
      {t("none")}
    </Badge>
  );
}

export function roleLabelKey(role: string | null): "admin" | "writer" | "reader" | "none" {
  return role === "admin" || role === "writer" || role === "reader" ? role : "none";
}
