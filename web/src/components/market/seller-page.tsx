"use client";

import { Banknote, CircleCheck, KeyRound, Package, Receipt, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { Fact, Facts, useMoney } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { useMarketFailure, useOrderAction, usePayout } from "./data";
import { mayPayOut, monthOf, previousMonth } from "./model";
import { LicenseBadge } from "./parts";
import {
  type Listing,
  revenueQuery,
  type SellerInfo,
  sellerInfoQuery,
  sellerListingsQuery,
  type SellerOrder,
  sellerOrdersQuery,
} from "./queries";

const MONTHS_SHOWN = 12;

function CardHead({ icon: Icon, title, description }: { icon: typeof Package; title: string; description?: string }) {
  return (
    <CardHeader>
      <CardTitle>
        <h2 className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </h2>
      </CardTitle>
      {description ? <CardDescription>{description}</CardDescription> : null}
    </CardHeader>
  );
}

function KeyCard({ info }: { info: SellerInfo }) {
  const t = useTranslations("market.seller");
  return (
    <Card className="min-w-0" data-testid="seller-key">
      <CardHead icon={KeyRound} title={t("keyTitle")} description={t("keyDescription")} />
      <Facts className="border-t">
        <Fact label={t("sellerId")}>{info.seller ? <Identifier value={info.seller} /> : t("unset")}</Fact>
        <Fact label={t("keyId")}>{info.key_id ? <span className="font-mono text-xs">{info.key_id}</span> : t("unset")}</Fact>
        <Fact label={t("publicKey")}>{info.public_key ? <span className="font-mono text-xs">{info.public_key}</span> : t("unset")}</Fact>
        <Fact label={t("signals")}>{info.signal_project ?? t("unset")}</Fact>
      </Facts>
    </Card>
  );
}

function ListingsCard({ listings }: { listings: Listing[] }) {
  const t = useTranslations("market.seller.listings");
  const money = useMoney();
  return (
    <Card className="min-w-0" data-testid="seller-listings">
      <CardHead icon={Package} title={t("title")} description={t("description")} />
      <CardContent className="min-w-0">
        {listings.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="flex flex-col divide-y">
            {listings.map((listing) => (
              <li key={listing.plugin} className="flex min-w-0 flex-col gap-1 py-2.5 first:pt-0 last:pb-0" data-listing={listing.plugin}>
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-mono text-xs font-medium">{listing.plugin}</span>
                  <span className="text-xs text-muted-foreground">{t(`state.${listing.state}`)}</span>
                </div>
                <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {t("line", {
                    publisher: listing.publisher,
                    price: money(listing.usd_per_hub_month),
                    version: listing.bundles[0]?.version ?? t("noBundle"),
                    count: listing.active_licenses,
                  })}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

type Acting = { order: SellerOrder; kind: "paid" | "cancel" };

function OrderDialog({ acting, onClose }: { acting: Acting | null; onClose: () => void }) {
  const t = useTranslations("market.seller.orders");
  const id = useId();
  const action = useOrderAction();
  const failure = useMarketFailure();
  const [invoice, setInvoice] = useState("");
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const paying = acting?.kind === "paid";

  const close = () => {
    if (action.isPending) return;
    setInvoice("");
    setRefused(null);
    onClose();
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!acting || (paying && !invoice.trim())) return;
    setRefused(null);
    try {
      const done = await action.mutateAsync({ number: acting.order.number, invoice: paying ? invoice.trim() : null });
      notify({
        tone: "success",
        text: paying ? t("markedPaid", { number: done.number }) : t("cancelled", { number: done.number }),
        description: paying && done.valid_until ? t("licensed", { day: done.valid_until }) : null,
      });
      setInvoice("");
      onClose();
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <Dialog open={acting !== null} onOpenChange={(open) => (!open ? close() : null)}>
      <DialogContent className="sm:max-w-md" data-testid="seller-order-dialog">
        <DialogHeader>
          <DialogTitle>{paying ? t("paidTitle", { number: acting?.order.number ?? "" }) : t("cancelTitle", { number: acting?.order.number ?? "" })}</DialogTitle>
          <DialogDescription>{paying ? t("paidDescription") : t("cancelDescription")}</DialogDescription>
        </DialogHeader>
        <form className="flex min-w-0 flex-col gap-4" onSubmit={(event) => void submit(event)} noValidate>
          {paying ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${id}-invoice`}>{t("invoice")}</Label>
              <Input
                id={`${id}-invoice`}
                value={invoice}
                onChange={(event) => setInvoice(event.target.value)}
                autoComplete="off"
                maxLength={100}
                data-testid="seller-order-invoice"
              />
            </div>
          ) : null}
          {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
          <DialogFooter>
            <Button type="button" variant="outline" size="lg" onClick={close} disabled={action.isPending}>
              {t("back")}
            </Button>
            <Button
              type="submit"
              size="lg"
              variant={paying ? "default" : "destructive"}
              busy={action.isPending}
              disabled={paying && !invoice.trim()}
              data-testid="seller-order-confirm"
            >
              {paying ? t("markPaid") : t("cancel")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function OrdersCard({ orders }: { orders: SellerOrder[] }) {
  const t = useTranslations("market.seller.orders");
  const money = useMoney();
  const [acting, setActing] = useState<Acting | null>(null);
  return (
    <Card className="min-w-0" data-testid="seller-orders">
      <CardHead icon={Receipt} title={t("title")} description={t("description")} />
      <CardContent className="min-w-0">
        {orders.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="flex flex-col divide-y">
            {orders.map((order) => (
              <li key={order.number} className="flex min-w-0 flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-start" data-order={order.number}>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-medium">{order.number}</span>
                    <LicenseBadge state={order.state} />
                  </div>
                  <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    {t("line", { plugin: order.plugin, months: order.months, total: money(order.usd_total), buyer: order.buyer_name, email: order.buyer_email })}
                  </span>
                  <span className="font-mono text-xs text-fg-subtle [overflow-wrap:anywhere]">{order.buyer_url ?? order.buyer_hub}</span>
                  <span className="text-xs text-fg-subtle">
                    {order.invoice ? t("paidLine", { invoice: order.invoice, until: order.valid_until ?? "" }) : null} <When value={order.closed_at ?? order.created_at} />
                  </span>
                </div>
                {order.state === "open" ? (
                  <div className="flex gap-1.5">
                    <Button size="sm" onClick={() => setActing({ order, kind: "paid" })} data-testid="seller-order-paid">
                      <CircleCheck aria-hidden="true" />
                      {t("markPaid")}
                    </Button>
                    <Button size="sm" variant="quiet-danger" onClick={() => setActing({ order, kind: "cancel" })} data-testid="seller-order-cancel">
                      {t("cancel")}
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
      <OrderDialog acting={acting} onClose={() => setActing(null)} />
    </Card>
  );
}

function monthsBack(now: Date): string[] {
  const months = [monthOf(now)];
  while (months.length < MONTHS_SHOWN) months.push(previousMonth(months[months.length - 1]));
  return months;
}

function RevenueCard() {
  const t = useTranslations("market.seller.revenue");
  const id = useId();
  const money = useMoney();
  const now = new Date();
  const [month, setMonth] = useState(monthOf(now));
  const state = useHubQuery(revenueQuery(browserApi, month));
  const payout = usePayout();
  const failure = useMarketFailure();

  const payOut = async (publisher: string) => {
    try {
      await payout.mutateAsync({ month, publisher });
      notify({ tone: "success", text: t("paidOut", { publisher, month }) });
    } catch (error) {
      notifyFailure(t("payoutFailed", { publisher }), failure(error));
    }
  };

  return (
    <Card className="min-w-0" data-testid="seller-revenue">
      <CardHead icon={Banknote} title={t("title")} description={t("description")} />
      <CardContent className="flex min-w-0 flex-col gap-4">
        <div className="flex min-w-0 flex-col gap-1.5 sm:max-w-48">
          <Label htmlFor={`${id}-month`}>{t("month")}</Label>
          <NativeSelect id={`${id}-month`} value={month} onChange={(event) => setMonth(event.target.value)} data-testid="seller-revenue-month">
            {monthsBack(now).map((value) => (
              <NativeSelectOption key={value} value={value}>
                {value}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <QueryView state={state} loading={<ListSkeleton rows={2} />}>
          {(revenue) => (
            <div className="flex min-w-0 flex-col gap-3">
              <p className="text-sm tabular-nums" data-testid="seller-revenue-total">
                {t("total", { gross: money(revenue.gross_usd), publishers: money(revenue.publisher_usd), platform: money(revenue.platform_usd) })}
              </p>
              {revenue.publishers.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("none")}</p>
              ) : (
                <ul className="flex flex-col divide-y">
                  {revenue.publishers.map((line) => (
                    <li key={line.publisher} className="flex min-w-0 flex-col gap-2 py-2.5 first:pt-0 last:pb-0 sm:flex-row sm:items-center" data-revenue-publisher={line.publisher}>
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        <span className="font-mono text-xs font-medium">{line.publisher}</span>
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {t("line", { orders: line.orders, gross: money(line.gross_usd), publisher: money(line.publisher_usd), platform: money(line.platform_usd) })}
                        </span>
                        {line.paid_out ? (
                          <span className="text-xs text-fg-subtle">
                            {t("paidOutBy", { login: line.paid_out.paid_by ?? "-" })} <When value={line.paid_out.paid_at} />
                          </span>
                        ) : null}
                      </div>
                      {mayPayOut(line, revenue.month, now) ? (
                        <Button size="sm" variant="outline" busy={payout.isPending} onClick={() => void payOut(line.publisher)} data-testid="seller-payout">
                          {t("markPaidOut")}
                        </Button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </QueryView>
      </CardContent>
    </Card>
  );
}

function SellerBody({ info }: { info: SellerInfo }) {
  const t = useTranslations("market.seller");
  const listings = useHubQuery(sellerListingsQuery(browserApi));
  const orders = useHubQuery(sellerOrdersQuery(browserApi));
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="seller-page">
      {!info.selling ? (
        <Alert data-testid="seller-not-selling">
          <TriangleAlert aria-hidden="true" />
          <AlertDescription>{t("notSelling")}</AlertDescription>
        </Alert>
      ) : null}
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <QueryView state={orders} loading={<ListSkeleton rows={3} />}>
            {(found) => <OrdersCard orders={found.orders} />}
          </QueryView>
          <RevenueCard />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <KeyCard info={info} />
          <QueryView state={listings} loading={<ListSkeleton rows={2} />}>
            {(found) => <ListingsCard listings={found.listings} />}
          </QueryView>
        </div>
      </div>
    </div>
  );
}

/**
 * /admin/market/seller, for hub admins of a hub that sells plugins (the feature seller, docs/market.md, Selling
 * plugins): the orders buying hubs placed, each open one to mark paid with its invoice number, which issues the
 * licence, or to cancel; a month's revenue by publisher, split 80/20, with each publisher's share to mark paid out by
 * hand once the month is over; the key that signs the licences, its public half only; and the listings.
 */
export function SellerPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("market.seller");
  const state = useHubQuery(sellerInfoQuery(browserApi), initialError);
  return (
    <>
      <PageHeader title={t("title")} sub={t("sub")} />
      <QueryView state={state} loading={<ListSkeleton rows={4} />}>
        {(info) => <SellerBody info={info} />}
      </QueryView>
    </>
  );
}
