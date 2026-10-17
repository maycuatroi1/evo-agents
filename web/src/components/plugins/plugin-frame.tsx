"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useEffect, useRef, useState } from "react";

import { browserApi } from "@/lib/api/browser";
import type { PluginPage, ProjectPlugin } from "@/lib/plugin-queries";
import { whoamiQuery } from "@/lib/queries";

import {
  allowedMethods,
  answer,
  BRIDGE,
  type BridgeContext,
  BridgeError,
  FRAME_HEIGHT,
  type Host,
  MAX_IN_FLIGHT,
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
 * does not reload the frame.
 */
export function PluginFrame({ project, plugin, page }: { project: string; plugin: ProjectPlugin; page: PluginPage }) {
  const t = useTranslations("plugins.frame");
  const locale = useLocale();
  const { resolvedTheme } = useTheme();
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const frame = useRef<HTMLIFrameElement>(null);
  const url = useRef(page.url ?? "");
  const [height, setHeight] = useState(FRAME_HEIGHT.initial);
  const theme: Theme = resolvedTheme === "dark" ? "dark" : "light";

  // What the handler reads when a message arrives, kept current without adding and removing the listener each render.
  const latest = useRef({ plugin, me, locale, theme });
  useEffect(() => {
    latest.current = { plugin, me, locale, theme };
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
      };
    };
    const host: Host = { context, resize: setHeight, hub };
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
      answer(request, latest.current.plugin.granted, host)
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
  }, [project, plugin.name, page.name]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage({ bridge: BRIDGE, event: "theme", theme }, "*");
  }, [theme]);

  return (
    <iframe
      ref={frame}
      title={t("title", { page: page.title, plugin: plugin.title })}
      sandbox="allow-scripts allow-forms"
      referrerPolicy="no-referrer"
      loading="eager"
      className="block w-full rounded-md border bg-card"
      style={{ height }}
      data-testid="plugin-frame"
    />
  );
}
