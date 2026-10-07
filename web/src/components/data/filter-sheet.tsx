"use client";

import { ListFilter, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { createContext, type FormEvent, type ReactNode, useContext, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

const InFilterSheet = createContext(false);

/** Whether a filter renders in the phone's filter sheet, where each group shows its label above its choices. */
export function useInFilterSheet(): boolean {
  return useContext(InFilterSheet);
}

/** The filters as a form, applied when it is submitted (the admin lists) rather than as each one changes. */
export type FilterSheetForm = {
  /** The search landmark's name. */
  label: string;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  /** Changes when the filters in the URL change, so the fields show them again (Back, Clear filters). */
  resetKey?: string;
  testId?: string;
};

type FilterSheetProps = {
  /** How many filters are in force: the button reads "Filters (2)" and shows as pressed. */
  active: number;
  /** What the list holds now ("3 runs match"), said again in the sheet as it changes. */
  summary?: ReactNode;
  onClear: () => void;
  /** Held by the caller, to close the sheet once a form applied; the sheet holds it otherwise. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  form?: FilterSheetForm;
  testId?: string;
  children: ReactNode;
};

/**
 * The kit's filters on a phone: one "Filters (n)" button in the toolbar, 44 px, that opens a sheet from the bottom with
 * every filter group, its label above its choices, and a foot with Clear filters and the way back to the list. Chips
 * apply as they are pressed, so the foot's button only shows the results; a form (`form`) applies when Filter is
 * pressed and then closes. The sheet is a dialog named "Filters": Esc, the close button or a tap outside close it, and
 * focus goes back to the button.
 */
export function FilterSheet({ active, summary, onClear, open, onOpenChange, form, testId, children }: FilterSheetProps) {
  const t = useTranslations("table.filters");
  const tAdmin = useTranslations("admin.filters");
  const [ownOpen, setOwnOpen] = useState(false);
  const shown = open ?? ownOpen;
  const setShown = onOpenChange ?? setOwnOpen;

  const clear = () => {
    onClear();
    setShown(false);
  };
  const body = (
    <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto overscroll-contain px-4 py-4">
      <InFilterSheet.Provider value>{children}</InFilterSheet.Provider>
    </div>
  );
  const foot = (
    <SheetFooter className="mt-0 shrink-0 flex-row flex-wrap items-center gap-2 border-t px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
      {summary ? (
        <p aria-live="polite" className="mr-auto min-w-0 text-xs leading-4 text-fg-subtle tabular-nums" data-testid="filter-sheet-summary">
          {summary}
        </p>
      ) : null}
      <div className="ml-auto flex items-center gap-2">
        {active > 0 ? (
          <Button type="button" variant="ghost" onClick={clear} data-testid="filter-sheet-clear">
            <X aria-hidden="true" />
            {t("clear")}
          </Button>
        ) : null}
        {form ? (
          <Button type="submit" data-testid={form.testId ? `${form.testId}-apply` : "filter-sheet-apply"}>
            <ListFilter aria-hidden="true" />
            {tAdmin("apply")}
          </Button>
        ) : (
          <SheetClose asChild>
            <Button type="button" data-testid="filter-sheet-done">
              {t("done")}
            </Button>
          </SheetClose>
        )}
      </div>
    </SheetFooter>
  );

  return (
    <Sheet open={shown} onOpenChange={setShown}>
      <SheetTrigger asChild>
        <Button
          type="button"
          variant="secondary"
          className={cn("shrink-0", active > 0 && "border-brand bg-surface-selected font-semibold text-foreground")}
          data-testid={testId ? `${testId}-open` : "filter-sheet-open"}
          data-active={active}
        >
          <ListFilter aria-hidden="true" />
          {active > 0 ? t("buttonActive", { count: active }) : t("button")}
        </Button>
      </SheetTrigger>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[85svh] gap-0 rounded-t-lg p-0"
        data-testid={testId ?? "filter-sheet"}
      >
        <div className="flex h-13 shrink-0 items-center gap-2 border-b px-4">
          <SheetTitle className="text-[15px] leading-[22px] font-semibold">{t("title")}</SheetTitle>
          <SheetDescription className="sr-only">{form ? t("descriptionForm") : t("description")}</SheetDescription>
          <SheetClose asChild>
            <Button type="button" variant="ghost" size="icon" className="ml-auto" aria-label={t("close")}>
              <X aria-hidden="true" />
            </Button>
          </SheetClose>
        </div>
        {form ? (
          <form
            key={form.resetKey}
            role="search"
            aria-label={form.label}
            noValidate
            onSubmit={form.onSubmit}
            className="flex min-h-0 flex-1 flex-col"
            data-testid={form.testId}
          >
            {body}
            {foot}
          </form>
        ) : (
          <>
            {body}
            {foot}
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

/**
 * The filters of a list's toolbar (`DataToolbar`): inline from 768 px, as they always were; under it, the "Filters (n)"
 * button and its sheet (`FilterSheet`), so the toolbar keeps to one row and the list starts in the first screen.
 */
export function ToolbarFilters(props: Omit<FilterSheetProps, "form" | "open" | "onOpenChange">) {
  const phone = useIsMobile();
  if (!phone) return <>{props.children}</>;
  return <FilterSheet {...props} />;
}

/**
 * A select or field among a toolbar's filters: its label for screen readers only inline, where the control's value
 * says what it is ("All projects"), and above it in the phone's sheet.
 */
export function ToolbarField({ label, children }: { label: string; children: (id: string) => ReactNode }) {
  const id = useId();
  const inSheet = useInFilterSheet();
  return (
    <div className={cn("flex min-w-0 flex-col gap-2", !inSheet && "contents")}>
      <Label htmlFor={id} className={inSheet ? "text-[13px] leading-[18px] font-medium text-foreground" : "sr-only"}>
        {label}
      </Label>
      {children(id)}
    </div>
  );
}
