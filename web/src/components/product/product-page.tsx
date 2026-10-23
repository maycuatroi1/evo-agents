import { ArrowRight, BookOpen, UserPlus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { BrandMark, GitHubMark } from "@/components/brand";
import { CommandLine } from "@/components/data/command-line";
import { Button } from "@/components/ui/button";

import { Claim, TEXT_LINK } from "./claim";
import { hubLoginCommand, INSTALL_COMMAND, PLUGIN_COMMANDS } from "./commands";
import { docHref, INVITE_FORM_URL, LICENSE, README, REPO_URL, ROADMAP } from "./links";
import { type Need, NEEDS } from "./needs";
import { Screenshot } from "./screenshot";
import { SourceList } from "./source-list";

type Props = {
  /** Where Sign in with GitHub goes: the API's login when the hub signs people in with GitHub, else /login, which says why not. */
  signInHref: string;
  /** The visitor holds a session cookie: Open the hub takes the place of Sign in. */
  member: boolean;
  /** The hub's URL for the CLI's sign-in. */
  hubUrl: string;
};

/** The page's column, 16 px from the edge on a phone and 24 px from 768 px (DESIGN.md). */
const COLUMN = "mx-auto w-full max-w-6xl px-4 md:px-6";

function Header() {
  const t = useTranslations("product.header");
  return (
    <header className="border-b">
      <div className={`${COLUMN} flex items-center justify-between gap-3 py-2.5`}>
        <span className="flex min-w-0 items-center gap-2.5">
          <BrandMark className="size-7" />
          <span className="text-[15px] font-semibold tracking-tight">evo-agents</span>
        </span>
        <nav aria-label={t("nav")}>
          <ul className="flex items-center gap-1">
            <li>
              <Button asChild variant="ghost">
                <a href={docHref(README)}>
                  <BookOpen aria-hidden="true" />
                  {t("docs")}
                </a>
              </Button>
            </li>
            <li>
              <Button asChild variant="ghost">
                <a href={REPO_URL} data-testid="product-source">
                  <GitHubMark />
                  {t("source")}
                </a>
              </Button>
            </li>
          </ul>
        </nav>
      </div>
    </header>
  );
}

function Hero({ signInHref, member }: Pick<Props, "signInHref" | "member">) {
  const t = useTranslations("product.hero");
  return (
    <section aria-labelledby="product-title" className={`${COLUMN} flex flex-col gap-6 py-12 md:py-20`}>
      <div className="flex max-w-3xl flex-col gap-4">
        <h1 id="product-title" className="text-[28px] leading-[34px] font-semibold tracking-tight text-balance">
          {t("title")}
        </h1>
        <Claim id="lede" className="max-w-prose text-[15px] leading-6 text-muted-foreground" />
      </div>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          {member ? (
            <Button asChild size="lg" className="h-11 text-base">
              <Link href="/" data-testid="product-open-hub">
                {t("openHub")}
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
          ) : (
            <Button asChild size="lg" className="h-11 text-base">
              <a href={signInHref} data-testid="product-sign-in">
                <GitHubMark className="size-5" />
                {t("signIn")}
              </a>
            </Button>
          )}
          <Button asChild size="lg" variant="outline" className="h-11 text-base">
            <a href={INVITE_FORM_URL} data-testid="product-invite">
              <UserPlus aria-hidden="true" />
              {t("invite")}
            </a>
          </Button>
        </div>
        <p className="max-w-prose text-xs text-muted-foreground">{t("inviteHint")}</p>
      </div>
    </section>
  );
}

function NeedRow({ need }: { need: Need }) {
  const t = useTranslations("product.needs");
  const row = `need-${need.key}`;
  return (
    <li>
      <article aria-labelledby={row} className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-10" data-testid={row}>
        <div className="flex min-w-0 flex-col gap-5">
          <h3 id={row} className="text-[15px] leading-[22px] font-semibold text-balance">
            {t(`rows.${need.key}.title`)}
          </h3>
          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-medium text-muted-foreground">{t("need")}</h4>
            <p className="text-sm text-pretty">{t(`rows.${need.key}.need`)}</p>
          </div>
          <SourceList sources={need.sources} checkedOn={need.checkedOn} />
          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-medium text-muted-foreground">{t("answer")}</h4>
            {need.claims.map((claim) => (
              <Claim key={claim} id={claim} />
            ))}
          </div>
        </div>
        <Screenshot image={need.image} alt={t(`rows.${need.key}.imageAlt`)} testId={`${row}-image`} />
      </article>
    </li>
  );
}

function Section({ id, title, children, description }: { id: string; title: string; children: ReactNode; description?: ReactNode }) {
  return (
    <section aria-labelledby={id} className="border-t">
      <div className={`${COLUMN} flex flex-col gap-8 py-12 md:py-16`}>
        <div className="flex max-w-3xl flex-col gap-2">
          <h2 id={id} className="text-xl font-semibold tracking-tight text-balance">
            {title}
          </h2>
          {description}
        </div>
        {children}
      </div>
    </section>
  );
}

function Needs() {
  const t = useTranslations("product.needs");
  return (
    <Section id="product-needs" title={t("title")} description={<p className="text-[13px] leading-[18px] text-muted-foreground">{t("demo")}</p>}>
      <ol className="flex flex-col gap-14">
        {NEEDS.map((need) => (
          <NeedRow key={need.key} need={need} />
        ))}
      </ol>
    </Section>
  );
}

function Step({ number, title, children }: { number: number; title: string; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 sm:gap-x-4">
      <span
        className="grid size-7 place-items-center rounded-full border bg-card text-[13px] font-semibold text-muted-foreground tabular-nums"
        aria-hidden="true"
      >
        {number}
      </span>
      <div className="flex min-w-0 flex-col gap-2.5">
        <h3 className="pt-0.5 text-[15px] leading-6 font-semibold text-balance">{title}</h3>
        {children}
      </div>
    </li>
  );
}

function GetStarted({ hubUrl }: Pick<Props, "hubUrl">) {
  const t = useTranslations("product.start");
  return (
    <Section id="product-start" title={t("title")} description={<p className="text-sm text-muted-foreground">{t("description")}</p>}>
      <ol className="flex max-w-3xl flex-col gap-8" data-testid="product-start">
        <Step number={1} title={t("install.title")}>
          <CommandLine command={INSTALL_COMMAND} label={t("install.label")} />
          <Claim id="install" className="text-muted-foreground" />
        </Step>
        <Step number={2} title={t("login.title")}>
          <CommandLine command={hubLoginCommand(hubUrl)} label={t("login.label")} testId="product-login-command" />
          <Claim id="login" className="text-muted-foreground" />
        </Step>
        <Step number={3} title={t("plugins.title")}>
          <div className="flex flex-col gap-1.5">
            {PLUGIN_COMMANDS.map((command, index) => (
              <CommandLine key={command} command={command} label={t("plugins.label", { number: index + 1 })} />
            ))}
          </div>
          <Claim id="plugins" className="text-muted-foreground" />
        </Step>
      </ol>
      <Claim id="next" className="max-w-3xl" />
    </Section>
  );
}

const DIRECTIONS = ["loop", "canvas", "growth"] as const;

function Direction() {
  const t = useTranslations("product.direction");
  return (
    <Section
      id="product-direction"
      title={t("title")}
      description={
        <>
          <Claim id="status" className="max-w-prose" />
          <Claim id="roadmap" className="max-w-prose" />
        </>
      }
    >
      <ul className="grid gap-4 md:grid-cols-3" data-testid="product-direction">
        {DIRECTIONS.map((item) => (
          <li key={item} className="flex flex-col gap-2 rounded-md border bg-card p-4 shadow-raised">
            <h3 className="text-[15px] leading-[22px] font-semibold">{t(item)}</h3>
            <Claim id={item} className="text-muted-foreground" />
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Footer() {
  const t = useTranslations("product.footer");
  const links = [
    { href: docHref(README), label: t("docs") },
    { href: REPO_URL, label: t("source") },
    { href: docHref(ROADMAP), label: t("roadmap") },
    { href: docHref(LICENSE), label: t("license") },
  ];
  return (
    <footer className="border-t">
      <div className={`${COLUMN} flex flex-col gap-3 py-6 md:flex-row md:items-center md:justify-between`}>
        <span className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <BrandMark className="size-5" />
          evo-agents
        </span>
        <nav aria-label={t("nav")}>
          <ul className="flex flex-wrap gap-x-5 text-[13px]">
            {links.map(({ href, label }) => (
              <li key={label}>
                <a href={href} className={`${TEXT_LINK} inline-flex items-center max-md:min-h-11`}>
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </footer>
  );
}

/**
 * The public page of evo-agents (PRODUCT_PATH, and `/` for a visitor without a session): what it does for which need,
 * each need with its public sources and each sentence about evo-agents linked to its docs, how to start, and where it
 * is going. It needs no session and shows nothing of the hub's data: the screens come from the demo hub.
 */
export function ProductPage({ signInHref, member, hubUrl }: Props) {
  const tApp = useTranslations("app");
  return (
    <div className="min-h-svh bg-background">
      <a
        href="#main"
        className="sr-only z-50 rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        {tApp("skipToContent")}
      </a>
      <Header />
      <main id="main" data-testid="product-page">
        <Hero signInHref={signInHref} member={member} />
        <Needs />
        <GetStarted hubUrl={hubUrl} />
        <Direction />
      </main>
      <Footer />
    </div>
  );
}
