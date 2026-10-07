import {
  dehydrate,
  type DehydratedState,
  QueryClient,
  QueryClientProvider,
  HydrationBoundary as QueryHydrationBoundary,
  useQuery,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { emptyEntries, HydrationBoundary } from "./hydration-boundary";

/**
 * Rendered with react-dom/server, as the server sends the page: the shell's observer comes first in the tree and
 * creates the entry, then the page's boundary hands over what the server component fetched.
 */
const KEY = ["workers"] as const;
const never = () => new Promise<string[]>(() => {});

function Shell() {
  const { data } = useQuery({ queryKey: KEY, queryFn: never });
  return <p>{data ? `fleet: ${data.length}` : "fleet: loading"}</p>;
}

function Page() {
  const { data } = useQuery({ queryKey: KEY, queryFn: never });
  return <main>{data ? data.join(", ") : "skeleton"}</main>;
}

async function prefetched(data: string[]): Promise<DehydratedState> {
  const server = new QueryClient();
  await server.prefetchQuery({ queryKey: KEY, queryFn: async () => data });
  return dehydrate(server);
}

function render(client: QueryClient, boundary: (children: ReactNode) => ReactNode): string {
  return renderToString(
    <QueryClientProvider client={client}>
      <Shell />
      {boundary(<Page />)}
    </QueryClientProvider>,
  );
}

describe("HydrationBoundary", () => {
  it("renders the page's prefetched data although the shell created its entry first", async () => {
    const state = await prefetched(["mini", "studio"]);
    const html = render(new QueryClient(), (children) => <HydrationBoundary state={state}>{children}</HydrationBoundary>);
    expect(html).toContain("<main>mini, studio</main>");
    // The shell rendered before the data arrived, here as in the browser's first render.
    expect(html).toContain("fleet: loading");
  });

  it("is needed: TanStack's boundary alone holds an existing entry back to an effect, so the page is a skeleton", async () => {
    const state = await prefetched(["mini"]);
    const html = render(new QueryClient(), (children) => <QueryHydrationBoundary state={state}>{children}</QueryHydrationBoundary>);
    expect(html).toContain("<main>skeleton</main>");
  });

  it("leaves an entry that holds data to TanStack's order, so a transition does not change the page it leaves", async () => {
    const client = new QueryClient();
    client.setQueryData(KEY, ["old"], { updatedAt: 1 });
    const state = await prefetched(["new"]);
    expect(emptyEntries(client, state)).toEqual([]);
    const html = render(client, (children) => <HydrationBoundary state={state}>{children}</HydrationBoundary>);
    expect(html).toContain("<main>old</main>");
  });

  it("fills only entries that exist without data, from queries that carry data", async () => {
    const client = new QueryClient();
    client.getQueryCache().build(client, { queryKey: KEY });
    client.getQueryCache().build(client, { queryKey: ["projects"] });
    const server = new QueryClient();
    await server.prefetchQuery({ queryKey: KEY, queryFn: async () => ["mini"] });
    await server.prefetchQuery({ queryKey: ["me"], queryFn: async () => ({ login: "octo" }) });
    const state = dehydrate(server);
    expect(emptyEntries(client, state).map((query) => query.queryKey)).toEqual([KEY]);
  });
});
