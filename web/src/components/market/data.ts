"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { pluginKeys } from "@/lib/plugin-queries";

import { marketKeys, sellerKeys } from "./queries";

/**
 * The writes of the market pages, each with the session's X-Evo-CSRF header: a hub admin's install or upgrade from a
 * market (trusting the publisher's signer when asked to), a project admin's request for one, and a hub admin's markets
 * and trusted signers. Each reloads the market, the store and the publishers whatever the hub answered; a 401 sends
 * the visitor to sign in.
 */

export type Install = { source: number; plugin: string; trust: boolean };
export type SourceIn = { repo: string; ref: string; path: string; token: string | null };

function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

function useRefresh() {
  const queryClient = useQueryClient();
  return useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: marketKeys.all });
    void queryClient.invalidateQueries({ queryKey: pluginKeys.store });
    void queryClient.invalidateQueries({ queryKey: pluginKeys.publishers });
  }, [queryClient]);
}

export function useMarketInstall() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async ({ source, plugin, trust }: Install) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      const params = { path: { source, plugin } };
      const body = { version: null, trust_publisher: trust };
      return call(api.POST("/v1/market/{source}/{plugin}/install", { params, body, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

export function useMarketRequest() {
  return useMutation({
    mutationFn: async ({ source, plugin, project }: { source: number; plugin: string; project: string }) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      const params = { path: { source, plugin } };
      return call(api.POST("/v1/market/{source}/{plugin}/request", { params, body: { project }, headers }));
    },
    onError: signInAgainOn401,
  });
}

export function useAddSource() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (body: SourceIn) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      return call(api.POST("/v1/market/sources", { body, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

export function useSourceAction() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async ({ id, action }: { id: number; action: "refresh" | "remove" }) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      const params = { path: { source_id: id } };
      if (action === "refresh") return call(api.POST("/v1/market/sources/{source_id}/refresh", { params, headers }));
      return call(api.DELETE("/v1/market/sources/{source_id}", { params, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

export function useRemoveTrust() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async (id: number) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      return call(api.DELETE("/v1/plugin-publishers/{entry_id}", { params: { path: { entry_id: id } }, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

export type Buy = { source: number; plugin: string; months: number; name: string; email: string; trust: boolean };

/** A hub admin's order of a paid plugin from its seller; the answer carries the order number and how to pay it. */
export function useMarketBuy() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async ({ source, plugin, months, name, email, trust }: Buy) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      const params = { path: { source, plugin } };
      const body = { months, contact: { name, email }, trust_publisher: trust };
      return call(api.POST("/v1/market/{source}/{plugin}/buy", { params, body, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

/** Ask the sellers about the open orders now, as the hourly job does. */
export function useLicenseCheck() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: async () => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      return call(api.POST("/v1/market/licenses/check", { headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

function useSellerRefresh() {
  const queryClient = useQueryClient();
  return useCallback(() => void queryClient.invalidateQueries({ queryKey: sellerKeys.all }), [queryClient]);
}

/** A seller admin marks an order paid with its invoice number, which issues the licence, or cancels an open one. */
export function useOrderAction() {
  const refresh = useSellerRefresh();
  return useMutation({
    mutationFn: async ({ number, invoice }: { number: string; invoice: string | null }) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      const params = { path: { number } };
      if (invoice === null) return call(api.POST("/v1/seller/orders/{number}/cancel", { params, headers }));
      return call(api.POST("/v1/seller/orders/{number}/paid", { params, body: { invoice }, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

/** Mark a publisher's share of a month paid out, by hand: the hub pays nobody itself. */
export function usePayout() {
  const refresh = useSellerRefresh();
  return useMutation({
    mutationFn: async ({ month, publisher }: { month: string; publisher: string }) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      return call(api.POST("/v1/seller/payouts", { body: { month, publisher, note: null }, headers }));
    },
    onError: signInAgainOn401,
    onSettled: refresh,
  });
}

/** What to say when a write of the market fails: the shared words, and the hub's own message beside them. */
export function useMarketFailure(): (error: unknown) => WriteFailure {
  const failure = useWriteFailure();
  const t = useTranslations("market.errors");
  return useCallback(
    (error: unknown) => {
      const found = failure(error, { 403: t("forbidden"), 409: t("conflict") });
      if (isApiError(error) && found.detail === null && error.info.message) {
        return { ...found, detail: error.info.message };
      }
      return found;
    },
    [failure, t],
  );
}
