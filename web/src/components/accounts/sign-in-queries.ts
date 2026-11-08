import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { signInAgainOn401 } from "./hooks";
import { SECRETS_PAGE_KEY } from "./login-queries";
import { accountKeys } from "./queries";

/**
 * Claude's sign-in from the page (`evo_agents/hub/server/claude_sign_in.py`): the hub gives the page of claude.ai to
 * approve on and a sealed flow; claude.ai then shows a code, and the hub trades the code and the flow for the token of
 * `claude setup-token`, which it keeps as the account. The page holds the flow and the pasted code, never a token.
 */
type Schemas = components["schemas"];
export type SignInStarted = Schemas["SignInStarted"];
export type SignInFinish = Schemas["SignInFinish"];
export type ClaudeSignedIn = Schemas["ClaudeSignedIn"];

/** A flow lasts 15 minutes on the hub; the page draws a new one before that, while the dialog is open. */
const FLOW_REFRESH_MS = 10 * 60 * 1000;

export function startSignIn(api: ApiSource) {
  return async (): Promise<SignInStarted> => {
    const client = api();
    return call(client.POST("/v1/accounts/claude-sign-in", { headers: await csrfHeaders(client) }));
  };
}

/**
 * The flow of the dialog: a new one each time it opens (`opening`) and every ten minutes. Opening claude.ai pins the flow of that
 * moment, so a refresh after the member left for claude.ai does not change the state the code they bring back is for.
 */
export function useSignInFlow(api: ApiSource, opening: number, enabled: boolean) {
  const flow = useQuery({
    queryKey: ["claude-sign-in", opening],
    queryFn: startSignIn(api),
    enabled,
    staleTime: FLOW_REFRESH_MS,
    gcTime: 0,
    refetchInterval: FLOW_REFRESH_MS,
    refetchOnWindowFocus: false,
    retry: 1,
  });
  const pinned = useRef<SignInStarted | null>(null);
  const [opened, setOpened] = useState(false);
  const open = useCallback(() => {
    if (flow.data) pinned.current = flow.data;
    setOpened(true);
  }, [flow.data]);
  const current = () => pinned.current ?? flow.data ?? null;
  /** After a refused code: the next click opens a fresh sign-in. */
  const restart = useCallback(() => {
    pinned.current = null;
    setOpened(false);
    void flow.refetch();
  }, [flow]);
  return { flow, opened, open, current, restart };
}

/** POST .../finish: the account written, or null and the status of the hub's refusal (0 without an answer), which the dialog shows from `error`. */
export function useFinishSignIn(api: ApiSource) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const finish = useCallback(
    async (body: SignInFinish): Promise<{ written: ClaudeSignedIn | null; status: number }> => {
      setPending(true);
      setError(null);
      let written: ClaudeSignedIn | null = null;
      let status = 0;
      try {
        const client = api();
        written = await call(client.POST("/v1/accounts/claude-sign-in/finish", { body, headers: await csrfHeaders(client) }));
      } catch (caught) {
        signInAgainOn401(caught);
        setError(caught);
        status = isApiError(caught) ? (caught.info.status ?? 0) : 0;
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: accountKeys.all }),
        queryClient.invalidateQueries({ queryKey: SECRETS_PAGE_KEY }),
      ]);
      setPending(false);
      return { written, status };
    },
    [api, queryClient],
  );
  const reset = useCallback(() => setError(null), []);
  return { finish, pending, error, reset };
}
