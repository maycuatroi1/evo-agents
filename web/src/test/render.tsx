import { render, type RenderOptions } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";

import messages from "../../messages/vi.json";

/** Render with the Vietnamese messages, as the app does for a visitor whose locale cookie says `vi`. */
export function renderVi(ui: ReactElement, options?: RenderOptions) {
  return render(
    <NextIntlClientProvider locale="vi" messages={messages} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
    options,
  );
}
