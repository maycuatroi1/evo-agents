// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/errors";

import messages from "../../../messages/en.json";

import { useOrgFailure } from "./hooks";

function wrapper({ children }: { children: ReactNode }) {
  return (
    <NextIntlClientProvider locale="en" messages={messages} timeZone="UTC">
      {children}
    </NextIntlClientProvider>
  );
}

const refused = (status: number, message: string) => new ApiError({ status, code: "refused", message, requestId: "req-1" });

describe("useOrgFailure", () => {
  it("puts the hub's reason under the page's words for a 403, 409 and 422", () => {
    const { result } = renderHook(() => useOrgFailure(), { wrapper });
    const failure = result.current;
    expect(failure(refused(403, "only a lead of a unit above web may change it"))).toEqual({
      text: "The hub refused this change.",
      detail: "Hub: only a lead of a unit above web may change it",
      requestId: "req-1",
      status: 403,
    });
    expect(failure(refused(409, "kim holds a live seat in web already; nothing changed")).detail).toBe("Hub: kim holds a live seat in web already; nothing changed");
    expect(failure(refused(422, "ends_on: before starts_on"))).toMatchObject({ text: "The hub could not accept these values.", detail: "Hub: ends_on: before starts_on" });
  });

  it("keeps a server failure without the hub's message", () => {
    const { result } = renderHook(() => useOrgFailure(), { wrapper });
    expect(result.current(refused(500, "Traceback")).detail).toBeNull();
  });
});
