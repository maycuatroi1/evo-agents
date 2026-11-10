// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { components } from "@/lib/api/schema";
import { chatKeys } from "@/lib/chat-keys";
import { useChatTitle } from "@/lib/use-chat-title";
import { type WorkCard, workKeys } from "@/lib/work";

type Chat = components["schemas"]["Chat"];

const api = vi.hoisted(() => ({ calls: 0 }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () => {
    api.calls += 1;
    throw new Error("the breadcrumb asks the hub nothing");
  },
}));

function chat(id: number, kind: Chat["kind"], title: string): Chat {
  return { id, kind, title } as Chat;
}

function card(title: string): WorkCard {
  return { title, status: "running", trace_id: 41 } as WorkCard;
}

function setup(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useChatTitle(path), { wrapper });
  return { client, hook };
}

describe("useChatTitle", () => {
  it("names a work chat by its work's title once the card is read, and by the chat's title before", async () => {
    const { client, hook } = setup("7");
    expect(hook.result.current).toBeNull();
    act(() => void client.setQueryData(chatKeys.one(7), chat(7, "work", "Work: ea-lead asks ea-dev")));
    await waitFor(() => expect(hook.result.current).toBe("Work: ea-lead asks ea-dev"));
    act(() => void client.setQueryData(workKeys.card(7), card("Fix the breadcrumb of work chats")));
    await waitFor(() => expect(hook.result.current).toBe("Fix the breadcrumb of work chats"));
    act(() => void client.setQueryData(workKeys.card(7), card("Fix the breadcrumb, retitled")));
    await waitFor(() => expect(hook.result.current).toBe("Fix the breadcrumb, retitled"));
    expect(api.calls).toBe(0);
  });

  it("keeps the chat's own title for a chat of any other kind", async () => {
    const { client, hook } = setup("8");
    act(() => {
      client.setQueryData(chatKeys.one(8), chat(8, "group", "Release talk"));
      // A card under a chat that is not a work chat names nothing.
      client.setQueryData(workKeys.card(8), card("Not this one"));
    });
    await waitFor(() => expect(hook.result.current).toBe("Release talk"));
    expect(api.calls).toBe(0);
  });

  it("names nothing for a part that is not a chat's id", () => {
    const { client, hook } = setup("new");
    act(() => void client.setQueryData(chatKeys.one(0), chat(0, "work", "Zero")));
    expect(hook.result.current).toBeNull();
  });
});
