"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import { isSubscription, liveShares, type ShareTarget } from "@/components/sharing/model";
import { SharesSection } from "@/components/sharing/shares-section";
import { useNow } from "@/hooks/use-now";

import { type Secret, secretKeys } from "./queries";

/** What a secret may be shared as: an env or git secret bound to a project; a forge secret drives the hub, never a run. */
export const secretTargets = (secrets: Secret[]): ShareTarget[] =>
  secrets
    .filter((secret) => secret.kind !== "forge" && secret.projects.length > 0)
    .map((secret) => ({ name: secret.name, projects: secret.projects, subscription: isSubscription(secret.kind, secret.env_var) }));

/** The live shares of the visitor's secrets under their list, with Share and Revoke. */
export function SecretSharing({ secrets }: { secrets: Secret[] }) {
  const queryClient = useQueryClient();
  const shares = useMemo(() => liveShares(secrets), [secrets]);
  // The clock runs only while a share ends some day: it says which end within a week.
  const now = useNow(shares.some((share) => share.expires_at !== null));
  const targets = useMemo(() => secretTargets(secrets), [secrets]);
  const onChanged = useCallback(
    () => Promise.all([queryClient.invalidateQueries({ queryKey: secretKeys.all }), queryClient.invalidateQueries({ queryKey: ["accounts"] })]),
    [queryClient],
  );
  return <SharesSection targets={targets} shares={shares} onChanged={onChanged} now={now} testId="secret-shares" />;
}
