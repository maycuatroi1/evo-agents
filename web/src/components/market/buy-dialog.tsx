"use client";

import { KeyRound, ShoppingCart } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { useMoney } from "@/components/data/facts";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { useMarketBuy, useMarketFailure } from "./data";
import { buyAction, isEmail, MAX_MONTHS, orderTotal, parseMonths } from "./model";
import { adminMarketHref, type Bought, type MarketPluginDetail } from "./queries";

/**
 * Buy or renew a paid plugin, for hub admins (docs/market.md, Selling plugins): the months the licence covers and who
 * the seller invoices. The order goes to the seller the market's index names, with this hub's id and the contact; the
 * dialog then shows the order's number and how to pay it. Once the seller marks it paid, the hub receives the licence
 * at its next hourly check and installs the plugin. Ordering from a publisher the hub does not trust yet trusts its
 * signer, which the dialog says first.
 */
export function BuyDialog({ plugin }: { plugin: MarketPluginDetail }) {
  const t = useTranslations("market.buy");
  const id = useId();
  const money = useMoney();
  const buy = useMarketBuy();
  const failure = useMarketFailure();
  const [open, setOpen] = useState(false);
  const [months, setMonths] = useState("1");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const [bought, setBought] = useState<Bought | null>(null);
  const action = buyAction(plugin);
  if (action === null) return null;

  const count = parseMonths(months);
  const total = orderTotal(plugin.price.usd_per_hub_month, count);
  const blocked = !plugin.trusted && !plugin.may_trust;
  const ready = count !== null && name.trim() !== "" && isEmail(email) && !blocked;

  const close = () => {
    setOpen(false);
    setRefused(null);
    setBought(null);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || count === null) return;
    setRefused(null);
    try {
      const done = await buy.mutateAsync({
        source: plugin.source,
        plugin: plugin.id,
        months: count,
        name: name.trim(),
        email: email.trim(),
        trust: !plugin.trusted,
      });
      setBought(done);
      notify({ tone: "success", text: t("ordered", { number: done.purchase.number }), link: { label: t("openLicenses"), href: adminMarketHref() } });
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <>
      <Button onClick={() => setOpen(true)} disabled={blocked} data-testid="market-buy">
        <ShoppingCart aria-hidden="true" />
        {action === "renew" ? t("renew") : t("open")}
      </Button>
      <Dialog open={open} onOpenChange={(next) => (!next && !buy.isPending ? close() : null)}>
        <DialogContent className="sm:max-w-md" data-testid="market-buy-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 [overflow-wrap:anywhere]">
              <KeyRound className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              {t("title", { name: plugin.name })}
            </DialogTitle>
            <DialogDescription>{bought ? t("orderedDescription") : t("description")}</DialogDescription>
          </DialogHeader>
          {bought ? (
            <div className="flex min-w-0 flex-col gap-3" data-testid="market-buy-done">
              <dl className="grid min-w-0 gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
                <dt className="text-[13px] text-muted-foreground">{t("number")}</dt>
                <dd className="min-w-0 font-mono text-xs" data-testid="market-buy-number">
                  {bought.purchase.number}
                </dd>
                <dt className="text-[13px] text-muted-foreground">{t("total")}</dt>
                <dd className="min-w-0 tabular-nums">{money(bought.purchase.usd_total)}</dd>
              </dl>
              <p className="text-[13px] text-muted-foreground">{t("instructions")}</p>
              <pre className="min-w-0 whitespace-pre-wrap rounded-md border bg-muted/40 p-3 text-xs [overflow-wrap:anywhere]" data-testid="market-buy-instructions">
                {bought.instructions}
              </pre>
              <DialogFooter>
                <Button size="lg" onClick={close}>
                  {t("done")}
                </Button>
              </DialogFooter>
            </div>
          ) : (
            <form className="flex min-w-0 flex-col gap-4" onSubmit={(event) => void submit(event)} noValidate>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${id}-months`}>{t("months")}</Label>
                <Input
                  id={`${id}-months`}
                  inputMode="numeric"
                  value={months}
                  onChange={(event) => setMonths(event.target.value)}
                  aria-invalid={count === null || undefined}
                  aria-describedby={`${id}-months-hint`}
                  data-testid="market-buy-months"
                />
                <p id={`${id}-months-hint`} className={count === null ? "text-xs text-danger" : "text-xs text-muted-foreground"}>
                  {count === null ? t("monthsInvalid", { max: MAX_MONTHS }) : t("monthsHint")}
                </p>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${id}-name`}>{t("name")}</Label>
                <Input id={`${id}-name`} value={name} onChange={(event) => setName(event.target.value)} autoComplete="organization" data-testid="market-buy-name" />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${id}-email`}>{t("email")}</Label>
                <Input
                  id={`${id}-email`}
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoComplete="email"
                  data-testid="market-buy-email"
                />
              </div>
              <p className="text-sm" data-testid="market-buy-total">
                {total === null ? t("noTotal") : t("totalLine", { total: money(total), months: count ?? 0 })}
              </p>
              {!plugin.trusted ? (
                <p className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-testid="market-buy-trust">
                  {t("trusts", { publisher: plugin.publisher.name ?? plugin.publisher.id, identity: plugin.publisher.identity })}
                </p>
              ) : null}
              {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
              <DialogFooter>
                <Button type="button" variant="outline" size="lg" onClick={close} disabled={buy.isPending}>
                  {t("cancel")}
                </Button>
                <Button type="submit" size="lg" busy={buy.isPending} disabled={!ready} data-testid="market-buy-confirm">
                  {buy.isPending ? t("ordering") : t("confirm")}
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
