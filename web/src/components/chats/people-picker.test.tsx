// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { ChatCandidate } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { matching, PeoplePicker, typedLogin } from "./people-picker";

const api = vi.hoisted(() => ({ people: [] as ChatCandidate[] | number, asked: [] as string[][] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        if (url.pathname === "/v1/chats/people") {
          api.asked.push(url.searchParams.getAll("agents"));
          if (typeof api.people === "number") return Response.json({ error: { code: "server", message: "down" } }, { status: api.people });
          return Response.json(api.people);
        }
        throw new Error(`unexpected ${request.method} ${url.pathname}`);
      },
    }),
}));

const person = (login: string, why: string | null = null): ChatCandidate => ({ login, avatar_url: null, why });

function Pick({ agents = [] as string[], full = false }) {
  const [picked, setPicked] = useState<string[]>([]);
  return (
    <>
      <PeoplePicker id="people" mode="pick" agents={agents} picked={picked} full={full} onPick={(login, on) => setPicked(on ? [...picked, login] : picked.filter((item) => item !== login))} />
      <output data-testid="picked">{picked.join(",")}</output>
    </>
  );
}

function setup(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const logins = () => screen.queryAllByTestId("people-option").map((row) => row.getAttribute("data-login"));
const picked = () => screen.getByTestId("picked").textContent;

beforeEach(() => {
  api.people = [person("lan"), person("mai"), person("minh", "no grant on project northwind")];
  api.asked = [];
});

describe("PeoplePicker", () => {
  it("reads a login from a search, @ left out, and matches logins ignoring case", () => {
    expect([typedLogin(" @Mai "), typedLogin("not a login"), typedLogin("")]).toEqual(["Mai", null, null]);
    expect(matching([person("lan"), person("Mai"), person("minh")], "M").map((p) => p.login)).toEqual(["Mai", "minh"]);
    expect(matching([person("lan")], "  ")).toHaveLength(1);
  });

  it("lists the people with the agents given, and one who could not be in the chat says why and is not picked", async () => {
    const user = userEvent.setup();
    setup(<Pick agents={["reviewer", "fixer", "reviewer"]} />);
    await waitFor(() => expect(logins()).toEqual(["lan", "mai", "minh"]));
    expect(api.asked).toEqual([["fixer", "reviewer"]]);
    const minh = screen.getAllByTestId("people-option")[2];
    expect(minh).toHaveTextContent("No grant on project northwind");
    expect(minh.querySelector("input")).toBeDisabled();
    await user.click(screen.getAllByTestId("people-option-check")[1]);
    expect(picked()).toBe("mai");
    await user.click(screen.getAllByTestId("people-option-check")[1]);
    expect(picked()).toBe("");
  });

  it("searches, and Enter takes the one row left or the login typed as it is", async () => {
    const user = userEvent.setup();
    setup(<Pick />);
    await waitFor(() => expect(logins()).toHaveLength(3));
    const search = screen.getByTestId("people-search");
    await user.type(search, "LA");
    expect(logins()).toEqual(["lan"]);
    await user.keyboard("{Enter}");
    expect([picked(), (search as HTMLInputElement).value]).toEqual(["lan", ""]);
    // nobody listed matches: the login typed is offered, and Enter adds it
    await user.type(search, "@octo-cat");
    expect(logins()).toEqual([]);
    expect(screen.getByTestId("people-option-typed")).toHaveAttribute("data-login", "octo-cat");
    await user.keyboard("{Enter}");
    expect(picked()).toBe("lan,octo-cat");
    // a search that is no login offers nothing and says so
    await user.type(search, "no one here");
    expect(screen.queryByTestId("people-option-typed")).toBeNull();
    expect(screen.getByTestId("people-empty")).toBeInTheDocument();
  });

  it("adds a person at once in a chat's members, leaving out who is in it", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    setup(<PeoplePicker id="people" mode="add" agents={[]} exclude={["LAN"]} onPick={onPick} />);
    await waitFor(() => expect(logins()).toEqual(["mai", "minh"]));
    const [mai, minh] = screen.getAllByTestId("people-option-add");
    expect(minh).toBeDisabled();
    await user.click(mai);
    expect(onPick).toHaveBeenCalledWith("mai", true);
  });

  it("still takes a login typed when the list cannot be read", async () => {
    api.people = 503;
    const user = userEvent.setup();
    setup(<Pick />);
    await waitFor(() => expect(screen.getByTestId("people-empty")).toBeInTheDocument());
    await user.type(screen.getByTestId("people-search"), "mai{Enter}");
    expect(picked()).toBe("mai");
  });
});
