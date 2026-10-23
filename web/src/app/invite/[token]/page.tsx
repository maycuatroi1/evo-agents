import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { InvitePage } from "@/components/invites/invite-page";
import { invitePreviewQuery } from "@/components/invites/queries";
import { PairingButton } from "@/components/workers/pairing-button";
import { call } from "@/lib/api/client";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { serverBranding } from "@/lib/hub-settings-server";
import { requestOrigin } from "@/lib/request-origin";

type Props = { params: Promise<{ token: string }> };

/** A title without the project or the inviter, and never indexed: the link is a credential. */
export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("invite");
  return { title: t("title"), robots: { index: false, follow: false } };
}

/** Whether the hub signs people in with GitHub now; false when it is not set up or does not answer. */
async function loginReady(): Promise<boolean> {
  try {
    const config = await call((await serverApi()).GET("/v1/auth/config"));
    return config.web_login;
  } catch {
    return false;
  }
}

export default async function InviteRoute({ params }: Props) {
  const { token } = await params;
  const api = await serverApi();
  const client = getQueryClient();
  const [error, ready, branding, origin, t, tApp] = await Promise.all([
    prefetch(client, invitePreviewQuery(() => api, token)),
    loginReady(),
    serverBranding(),
    requestOrigin(),
    getTranslations("invite.start.worker"),
    getTranslations("app"),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <InvitePage
        token={token}
        origin={origin}
        hubName={branding?.name ?? tApp("name")}
        logoUrl={branding?.logo_url ?? null}
        loginReady={ready}
        initialError={error}
        pairing={<PairingButton label={t("pair")} testId="invite-pair-worker" />}
      />
    </HydrationBoundary>
  );
}
