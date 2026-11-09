// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { usePageCrumb, usePageCrumbLabel } from "./page-crumb";

describe("page crumb", () => {
  it("names the crumb of the page that shows, for its path only, and takes the name back when it leaves", () => {
    const read = renderHook(({ path }) => usePageCrumbLabel(path), { initialProps: { path: "/p/demo/x/srs/cases/abc" } });
    expect(read.result.current).toBeNull();
    const page = renderHook(() => usePageCrumb("/p/demo/x/srs/cases/abc", "UC ORD-1"));
    expect(read.result.current).toBe("UC ORD-1");
    read.rerender({ path: "/p/demo/x/srs/reviews" });
    expect(read.result.current).toBeNull();
    read.rerender({ path: "/p/demo/x/srs/cases/abc" });
    act(() => page.unmount());
    expect(read.result.current).toBeNull();
  });
});
