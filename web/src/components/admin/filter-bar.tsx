"use client";

import { Filter, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { FormEvent, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

import { PAGE_SIZES } from "./data";

/**
 * The filters of a server-paged list, as the toolbar of its card (`DataCard`): a search form whose fields change
 * nothing until it is submitted (Enter in any field, or Filter), so a screen reader moving through a select does not
 * reload the list on every option. Fields sit in one row that wraps, each with its label above in `caption`; Filter
 * and Clear filters follow them, then the rows per page and the number of results on the right.
 */
export function FilterBar({
  label,
  onApply,
  onClear,
  canClear,
  children,
  footer,
  note,
  count,
  testId,
}: {
  label: string;
  onApply: (form: FormData) => void;
  onClear: () => void;
  canClear: boolean;
  children: ReactNode;
  /** A setting of the view rather than a filter (rows per page), on the right. */
  footer?: ReactNode;
  /** A sentence under the fields; give it an id for the fields it explains. */
  note?: ReactNode;
  /** The number of results, said again in a polite live region when it changes. */
  count?: ReactNode;
  testId: string;
}) {
  const t = useTranslations("admin.filters");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onApply(new FormData(event.currentTarget));
  };
  return (
    <form
      role="search"
      aria-label={label}
      onSubmit={submit}
      noValidate
      className="flex flex-col gap-2 border-b px-4 py-3"
      data-testid={testId}
    >
      <div className="flex flex-wrap items-end gap-x-3 gap-y-3">
        {children}
        <div className="flex items-center gap-2">
          <Button type="submit" variant="secondary" data-testid={`${testId}-apply`}>
            <Filter aria-hidden="true" />
            {t("apply")}
          </Button>
          {canClear ? (
            <Button type="button" variant="ghost" onClick={onClear}>
              <X aria-hidden="true" />
              {t("clear")}
            </Button>
          ) : null}
        </div>
        {footer || count ? (
          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
            {footer}
            {count ? (
              <p aria-live="polite" className="text-xs leading-4 text-fg-subtle tabular-nums" data-testid={`${testId}-count`}>
                {count}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
      {note ? <div className="text-xs leading-4 text-fg-subtle">{note}</div> : null}
    </form>
  );
}

/** A filter of the toolbar: its label in `caption` over a 32 px control (44 px under 768 px), then its hint and error. */
export function FilterField({
  id,
  label,
  error,
  hint,
  className,
  children,
}: {
  id: string;
  label: string;
  error?: string | null;
  hint?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex w-full min-w-0 flex-col gap-1 sm:w-44", className)}>
      <Label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
      </Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-fg-subtle">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The classes of a text field in the toolbar: 32 px, and 44 px with 16 px text under 768 px. */
export const FILTER_INPUT = "h-8 max-md:h-11";
/** The same for a NativeSelect, whose class names its wrapper. */
export const FILTER_SELECT = "w-full max-md:[&_select]:h-11 max-md:[&_select]:text-base";

/** What aria-describedby should name for a field: its hint and its error, when shown. */
export function describedBy(id: string, hint: boolean, error: string | null | undefined): string | undefined {
  return [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
}

/** A form value as text. */
export function field(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

/** Rows per page, read by the form as `limit`. */
export function PageSizeField({ id, value }: { id: string; value: number }) {
  const t = useTranslations("admin.filters");
  return (
    <div className="flex items-center gap-2">
      <Label htmlFor={id} className="text-xs font-normal text-muted-foreground">
        {t("pageSize")}
      </Label>
      <NativeSelect id={id} name="limit" defaultValue={String(value)} className="max-md:[&_select]:h-11 max-md:[&_select]:text-base">
        {PAGE_SIZES.map((size) => (
          <NativeSelectOption key={size} value={String(size)}>
            {size}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </div>
  );
}
