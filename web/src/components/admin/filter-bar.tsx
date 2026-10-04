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
 * The filters of a server-paged list, as a search form: fields change nothing until the form is submitted (Enter in
 * any field, or the button), so a screen reader moving through a select does not reload the list on every option.
 */
export function FilterBar({
  label,
  onApply,
  onClear,
  canClear,
  children,
  footer,
  note,
  testId,
}: {
  label: string;
  onApply: (form: FormData) => void;
  onClear: () => void;
  canClear: boolean;
  children: ReactNode;
  /** A setting of the view rather than a filter (rows per page), beside the buttons. */
  footer?: ReactNode;
  /** A sentence under the fields; give it an id for the fields it explains. */
  note?: ReactNode;
  testId: string;
}) {
  const t = useTranslations("admin.filters");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onApply(new FormData(event.currentTarget));
  };
  return (
    <form role="search" aria-label={label} onSubmit={submit} noValidate className="rounded-xl border bg-card p-4" data-testid={testId}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-[repeat(auto-fit,minmax(11rem,1fr))]">{children}</div>
      {note ? <div className="mt-3 text-xs text-muted-foreground">{note}</div> : null}
      <div className="mt-4 flex flex-wrap items-end gap-2 border-t pt-4">
        {footer ? <div className="mr-auto">{footer}</div> : <span className="mr-auto" />}
        {canClear ? (
          <Button type="button" variant="ghost" size="lg" onClick={onClear}>
            <X aria-hidden="true" />
            {t("clear")}
          </Button>
        ) : null}
        <Button type="submit" size="lg" data-testid={`${testId}-apply`}>
          <Filter aria-hidden="true" />
          {t("apply")}
        </Button>
      </div>
    </form>
  );
}

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
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

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
      <Label htmlFor={id} className="font-normal text-muted-foreground">
        {t("pageSize")}
      </Label>
      <NativeSelect id={id} name="limit" defaultValue={String(value)} className="[&_select]:h-9">
        {PAGE_SIZES.map((size) => (
          <NativeSelectOption key={size} value={String(size)}>
            {size}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </div>
  );
}
