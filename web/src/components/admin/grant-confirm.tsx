"use client";

import { ArrowLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import type { RefObject } from "react";

import { VisibilityLevel } from "@/components/data/visibility";
import { RoleBadge } from "@/components/shell/role-badge";
import { Button } from "@/components/ui/button";
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

import type { Role, UserGrant } from "./data";
import { InlineError, type WriteFailure } from "./notice";

/** The grant's second step: what will be written, the grant it replaces, Back and Confirm. */
export function GrantConfirm({
  heading,
  name,
  project,
  role,
  maxLevel,
  existing,
  roleName,
  levelName,
  pending,
  error,
  onBack,
  onConfirm,
}: {
  heading: RefObject<HTMLHeadingElement | null>;
  name: string;
  project: string;
  role: Role;
  maxLevel: string;
  existing: UserGrant | undefined;
  roleName: (value: string) => string;
  levelName: (level: string) => string;
  pending: boolean;
  error: WriteFailure | null;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("admin.grant");
  return (
    <div className="flex flex-col gap-5" aria-busy={pending || undefined}>
      <DialogHeader>
        <DialogTitle asChild>
          <h2 ref={heading} tabIndex={-1} className="outline-none">
            {existing ? t("confirmChangeTitle") : t("confirmTitle")}
          </h2>
        </DialogTitle>
        <DialogDescription>{t("confirmDescription")}</DialogDescription>
      </DialogHeader>
      <dl
        className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2.5 rounded-md border bg-muted/40 px-3 py-3 text-sm"
        data-testid="grant-summary"
      >
        <dt className="text-muted-foreground">{t("summary.login")}</dt>
        <dd className="min-w-0 font-mono font-medium break-all">{name}</dd>
        <dt className="text-muted-foreground">{t("summary.project")}</dt>
        <dd className="min-w-0 font-mono font-medium break-all">{project}</dd>
        <dt className="text-muted-foreground">{t("summary.role")}</dt>
        <dd>
          <RoleBadge role={role} />
        </dd>
        <dt className="text-muted-foreground">{t("summary.visibility")}</dt>
        <dd className="font-medium">
          <VisibilityLevel level={maxLevel} />
        </dd>
        {existing ? (
          <>
            <dt className="text-muted-foreground">{t("summary.previous")}</dt>
            <dd className="text-muted-foreground">{t("previous", { role: roleName(existing.role), level: levelName(existing.max_level) })}</dd>
          </>
        ) : null}
      </dl>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          size="lg"
          onClick={() => {
            if (!pending) onBack();
          }}
          aria-disabled={pending || undefined}
          className="aria-disabled:opacity-50"
        >
          <ArrowLeft aria-hidden="true" />
          {t("back")}
        </Button>
        <Button
          type="button"
          size="lg"
          onClick={onConfirm}
          busy={pending}
          data-testid="grant-confirm"
        >
          {pending ? t("saving") : t("confirm")}
        </Button>
      </DialogFooter>
    </div>
  );
}
