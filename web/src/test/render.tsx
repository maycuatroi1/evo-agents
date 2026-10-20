import { render, type RenderOptions } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";

import { HubToaster } from "@/components/feedback/toast";
import { TooltipProvider } from "@/components/ui/tooltip";

import messages from "../../messages/vi.json";

/**
 * Render with the Vietnamese messages, as the app does for a visitor whose locale cookie says `vi`, inside the app's
 * tooltip provider and beside its toaster, so what a write or a copy says in a toast is in the document.
 */
export function renderVi(ui: ReactElement, options?: RenderOptions) {
  return render(
    <NextIntlClientProvider locale="vi" messages={messages} timeZone="Asia/Ho_Chi_Minh">
      <TooltipProvider>{ui}</TooltipProvider>
      <HubToaster />
    </NextIntlClientProvider>,
    options,
  );
}
