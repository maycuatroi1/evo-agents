import type { ApiError } from "@/lib/api/errors";
import type { Locale } from "@/i18n/locales";

import type messages from "../messages/vi.json";

declare module "next-intl" {
  interface AppConfig {
    Locale: Locale;
    Messages: typeof messages;
  }
}

declare module "@tanstack/react-query" {
  interface Register {
    defaultError: ApiError;
  }
}
