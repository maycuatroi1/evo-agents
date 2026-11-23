"use client";

import { useTranslations } from "next-intl";

import type { Decision } from "./queries";

type ChannelDelivery = NonNullable<Decision["channel_deliveries"]>[number];

/**
 * A decision's label and where its message stands on each channel of a core plugin (`channel_deliveries`): sent, sent
 * as a link alone because its label keeps its content off the channel, not sent yet and tried again, not sent and why,
 * or failed with the last error, and the edit of the message once the decision closed. docs/notifications.md
 * (Decisions on channels) is the design. A hub before schema 0265 sends neither: nothing shows.
 */
export function DecisionChannels({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  const channels = decision.channel_deliveries ?? [];
  if (!decision.label && channels.length === 0) return null;
  return (
    <div className="flex flex-col gap-1 text-xs text-muted-foreground" data-testid="decision-channels">
      {decision.label ? (
        <p data-testid="decision-label" data-given={decision.label_given ? "true" : "false"}>
          {t(decision.label_given ? "label" : "labelDefault", { label: decision.label })}
        </p>
      ) : null}
      {channels.length > 0 ? (
        <ul aria-label={t("channelsTitle")} className="flex flex-col gap-0.5">
          {channels.map((entry) => (
            <li key={entry.channel} data-testid="decision-channel" data-channel={entry.channel} data-state={entry.state} className="[overflow-wrap:anywhere]">
              <ChannelLine entry={entry} label={decision.label ?? "-"} />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function ChannelLine({ entry, label }: { entry: ChannelDelivery; label: string }) {
  const t = useTranslations("inbox.decision");
  const channel = entry.channel;
  let text: string;
  if (entry.state === "sent") {
    text = entry.withheld ? t(`channelWithheld.${entry.withheld}`, { channel, label }) : t("channelSent", { channel });
  } else if (entry.state === "not_sent") {
    text = t("channelNotSent", { channel, reason: entry.reason ?? "-" });
  } else if (entry.state === "failed") {
    text = t("channelFailed", { channel, error: entry.error ?? "-" });
  } else {
    text = t("channelPending", { channel, error: entry.error ?? "none" });
  }
  const edit = entry.edit;
  return (
    <>
      {text}
      {edit ? <span data-testid="decision-channel-edit"> {t(`channelEdit.${edit.state}`, { error: edit.error ?? "-" })}</span> : null}
    </>
  );
}
