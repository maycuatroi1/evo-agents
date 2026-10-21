import type { ReactNode } from "react";
import { useTranslations } from "next-intl";

import { DataCard } from "@/components/data/data-card";
import { Badge } from "@/components/ui/badge";

import type { AccountGroupId } from "./model";

/**
 * One group of the Accounts page: its title with the number of accounts, a line saying what the group is, and its
 * table, or a line saying there is none while the other group has some. A new group of accounts is one more of these.
 */
export function AccountGroup({ id, count, children }: { id: AccountGroupId; count: number; children: ReactNode }) {
  const t = useTranslations("accounts.groups");
  return (
    <section aria-labelledby={`accounts-group-${id}`} className="flex flex-col gap-3" data-testid={`accounts-group-${id}`}>
      <header className="flex flex-col gap-0.5">
        <div className="flex items-center gap-2">
          <h2 id={`accounts-group-${id}`} className="text-base font-semibold">
            {t(`${id}.title`)}
          </h2>
          <Badge variant="secondary" data-testid={`accounts-group-${id}-count`}>
            {count}
          </Badge>
        </div>
        <p className="text-sm text-fg-muted">{t(`${id}.description`)}</p>
      </header>
      {count > 0 ? (
        <DataCard>{children}</DataCard>
      ) : (
        <p className="rounded-md border border-dashed px-3 py-4 text-sm text-fg-subtle" data-testid={`accounts-group-${id}-empty`}>
          {t(`${id}.none`)}
        </p>
      )}
    </section>
  );
}
