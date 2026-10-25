"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Route } from "next";
import { useRouter, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useEffect, useMemo, useRef, useState } from "react";

import { notify } from "@/components/feedback/toast";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { pluginOn } from "@/lib/plugin-pages";
import { type PluginPage, type ProjectPlugin, projectPluginQuery } from "@/lib/plugin-queries";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import {
  allowedMethods,
  answer,
  BRIDGE,
  type BridgeContext,
  BridgeError,
  FRAME_HEIGHT,
  type Host,
  KIT,
  MAX_IN_FLIGHT,
  type PageLocation,
  readRequest,
  reply,
  type Theme,
} from "./bridge";
import { hubReader } from "./bridge-hub";

/**
 * The frame of an isolated plugin's page (web/DESIGN.md, Plugins): `sandbox="allow-scripts allow-forms"` and never
 * allow-same-origin, so the page has an opaque origin, reads no cookie or storage of the hub, cannot reach the page
 * around it, and its own requests carry no session. The hub serves its files under a CSP of the same sandbox with
 * connect-src 'none' (evo_agents.hub.server.plugin_ui). The frame's one way to the hub is the bridge evo-plugin/1:
 * a message from this frame's window, from an opaque origin, answered by calling the hub with the viewer's session for
 * the methods the project granted the plugin (bridge.ts). The answer goes back with the target "*", the only one an
 * opaque origin can be posted to; it goes to the frame's own window, never to another.
 *
 * The URL is the one the hub signed when the page opened, set once the bridge listens; a later read of the plugins
 * does not reload the frame. What the page may call is what the project grants the plugin now: before a method that
 * needs a permission, the bridge reads the plugin again when the last read is over GRANT_FRESH_MS old (`grantOf`), and
 * the hub checks every call too (header X-Evo-Plugin). A read that finds the plugin off, removed or out of the viewer's
 * reach refuses the call, and the page around the frame, which reads the same query, stops showing it.
 */
export const GRANT_FRESH_MS = 5_000;

/** What the project grants plugin `name` now, read again from the hub when the cached read is over GRANT_FRESH_MS old. */
export async function grantOf(client: QueryClient, project: string, name: string): Promise<readonly string[]> {
  try {
    const plugin = await client.fetchQuery({ ...projectPluginQuery(browserApi, project, name), staleTime: GRANT_FRESH_MS });
    return pluginOn(plugin) ? plugin.granted : [];
  } catch (error) {
    if (isApiError(error) && error.kind !== "network" && error.kind !== "server") return []; // removed, or no access
    throw new BridgeError("hub_unavailable", "the hub did not answer; try again shortly");
  }
}

/**
 * `path` is the sub path after the page's name in the hub's URL (/p/{project}/x/{plugin}/{page}/{path}), "" for none;
 * the page reads it and the URL's query as `location` of its context, and gets the event `location` when either
 * changes while this frame stays. A `navigate` that the router answers with a new page segment frames the page
 * afresh, and it reads its new location from the context.
 *
 * `panel` frames a page of a slot (a panel on another page of the hub, such as the Decisions page): no border, no
 * least height, and no room at all, out of the flow and unseen, while the page reports a height of 0 or none yet.
 */
export function PluginFrame({
  project,
  plugin,
  page,
  path = "",
  panel = false,
}: {
  project: string;
  plugin: ProjectPlugin;
  page: PluginPage;
  path?: string;
  panel?: boolean;
}) {
  const t = useTranslations("plugins.frame");
  const client = useQueryClient();
  const locale = useLocale();
  const router = useRouter();
  const search = useSearchParams();
  const { resolvedTheme } = useTheme();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const frame = useRef<HTMLIFrameElement>(null);
  const url = useRef(page.url ?? "");
  const [height, setHeight] = useState(panel ? 0 : FRAME_HEIGHT.initial);
  const theme: Theme = resolvedTheme === "dark" ? "dark" : "light";
  const searchText = search?.toString() ?? "";
  const location = useMemo<PageLocation>(
    () => ({ path, query: Object.fromEntries(new URLSearchParams(searchText)) }),
    [path, searchText],
  );

  // What the handler reads when a message arrives, kept current without adding and removing the listener each render.
  const latest = useRef({ plugin, me, locale, theme, location, router, t });
  useEffect(() => {
    latest.current = { plugin, me, locale, theme, location, router, t };
  });

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    const hub = hubReader(browserApi(), project, plugin.name);
    let inFlight = 0;
    const context = (): BridgeContext => {
      const { plugin: current, me: viewer, locale: lang, theme: shade } = latest.current;
      const role = viewer?.grants.find((grant) => grant.project === project)?.role ?? null;
      return {
        bridge: BRIDGE,
        project,
        plugin: current.name,
        page: page.name,
        viewer: { login: viewer?.login ?? "", role },
        locale: lang,
        theme: shade,
        methods: allowedMethods(current.granted),
        location: latest.current.location,
        kit: KIT,
      };
    };
    const host: Host = {
      context,
      // A page's own frame never gets less than its least height; a panel shrinks to nothing.
      resize: (next) => setHeight(panel ? next : Math.max(next, FRAME_HEIGHT.min)),
      minHeight: panel ? 0 : FRAME_HEIGHT.min,
      granted: () => grantOf(client, project, plugin.name),
      hub,
      navigate: (to) => latest.current.router.push(to as Route),
      toast: (text, tone) =>
        notify({ tone, text, description: latest.current.t("toastFrom", { plugin: latest.current.plugin.title }) }),
    };
    const post = (message: unknown) => element.contentWindow?.postMessage(message, "*");

    const onMessage = (event: MessageEvent) => {
      // Only this frame's window, holding a document of an opaque origin: anything else is not the plugin's page.
      if (event.source === null || event.source !== element.contentWindow || event.origin !== "null") return;
      const request = readRequest(event.data);
      if (request === null) return;
      if ("error" in request) {
        post(reply(request.id, { error: request.error }));
        return;
      }
      if (inFlight >= MAX_IN_FLIGHT) {
        post(reply(request.id, { error: new BridgeError("busy", `at most ${MAX_IN_FLIGHT} requests at once`) }));
        return;
      }
      inFlight += 1;
      answer(request, host)
        .then((result) => post(reply(request.id, { result })))
        .catch((error: unknown) => post(reply(request.id, { error })))
        .finally(() => {
          inFlight -= 1;
        });
    };
    window.addEventListener("message", onMessage);
    // The page loads only now, once its requests have someone to answer them: a frame the server rendered with its
    // URL would run the plugin's script before the web hydrated, and its first requests would be lost.
    if (!element.getAttribute("src")) element.setAttribute("src", url.current);
    return () => window.removeEventListener("message", onMessage);
  }, [client, project, plugin.name, page.name, panel]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage({ bridge: BRIDGE, event: "theme", theme }, "*");
  }, [theme]);

  // The first location is in the context; only a later one is news to the page.
  const shownLocation = useRef(location);
  useEffect(() => {
    if (shownLocation.current === location) return;
    shownLocation.current = location;
    frame.current?.contentWindow?.postMessage({ bridge: BRIDGE, event: "location", location }, "*");
  }, [location]);

  return (
    <iframe
      ref={frame}
      title={t("title", { page: page.title, plugin: plugin.title })}
      sandbox="allow-scripts allow-forms"
      referrerPolicy="no-referrer"
      loading="eager"
      className={panel ? cn("block w-full bg-transparent", height === 0 && "pointer-events-none invisible absolute inset-x-0") : "block w-full rounded-md border bg-card"}
      style={{ height }}
      aria-hidden={panel && height === 0 ? true : undefined}
      tabIndex={panel && height === 0 ? -1 : undefined}
      data-testid="plugin-frame"
      data-panel={panel ? (height === 0 ? "empty" : "shown") : undefined}
    />
  );
}
