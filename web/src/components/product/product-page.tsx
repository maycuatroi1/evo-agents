import { ArrowRight, BookOpen, UserPlus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { BrandMark, GitHubMark } from "@/components/brand";
import { CommandLine } from "@/components/data/command-line";
import { Button } from "@/components/ui/button";

import { Claim, TEXT_LINK } from "./claim";
import { hubLoginCommand, INSTALL_COMMAND } from "./commands";
import { docHref, GETTING_STARTED, INVITE_FORM_URL, LICENSE, LOCAL_GRAPH, REPO_URL, ROADMAP } from "./links";
import { NEEDS } from "./needs";
import { Screenshot } from "./screenshot";

type Props = {
  /** The API's GitHub login, or /login when this hub has no GitHub sign-in configured. */
  signInHref: string;
  member: boolean;
  /** This hub's origin, used by the copyable CLI command. */
  hubUrl: string;
};

const COLUMN = "mx-auto w-full max-w-6xl px-4 md:px-6";

function Header() {
  const t = useTranslations("product.header");
  return (
    <header className="border-b">
      <div className={`${COLUMN} flex items-center justify-between gap-3 py-2.5`}>
        <span className="flex min-w-0 items-center gap-2.5">
          <BrandMark className="size-7 shrink-0" />
          <span className="text-[15px] font-semibold tracking-tight whitespace-nowrap">evo-agents</span>
        </span>
        <nav aria-label={t("nav")}>
          <ul className="flex items-center gap-1">
            <li><Button asChild variant="ghost"><a href={docHref(GETTING_STARTED)}><BookOpen aria-hidden="true" />{t("docs")}</a></Button></li>
            <li><Button asChild variant="ghost"><a href={REPO_URL} data-testid="product-source"><GitHubMark /><span className="sr-only sm:not-sr-only">{t("source")}</span></a></Button></li>
          </ul>
        </nav>
      </div>
    </header>
  );
}

function Hero({ signInHref, member }: Pick<Props, "signInHref" | "member">) {
  const t = useTranslations("product.hero");
  return (
    <section aria-labelledby="product-title" className={`${COLUMN} grid items-center gap-8 py-12 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-10 lg:py-16`}>
      <div className="flex min-w-0 flex-col gap-6">
        <h1 id="product-title" className="text-[32px] leading-tight font-semibold tracking-tight text-balance md:text-4xl">{t("title")}</h1>
        <Claim id="lede" className="text-base leading-7 text-muted-foreground" />
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
          {member ? (
            <Button asChild size="lg" className="h-11 text-base">
              <Link href="/" data-testid="product-open-hub">{t("openHub")}<ArrowRight aria-hidden="true" /></Link>
            </Button>
          ) : (
            <Button asChild size="lg" className="h-11 text-base">
              <a href="#product-start" data-testid="product-get-started">{t("start")}<ArrowRight aria-hidden="true" /></a>
            </Button>
          )}
          <Button asChild size="lg" variant="outline" className="h-11 text-base">
            {member ? (
              <a href="#product-start" data-testid="product-get-started">{t("start")}</a>
            ) : (
              <a href={signInHref} data-testid="product-sign-in"><GitHubMark className="size-5" />{t("signIn")}</a>
            )}
          </Button>
        </div>
      </div>
      <figure className="min-w-0">
        <Screenshot image="monitor" alt={t("imageAlt")} testId="product-demo" />
        <figcaption className="mt-3 text-xs text-muted-foreground">{t("demo")}</figcaption>
      </figure>
    </section>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="border-t">
      <div className={`${COLUMN} flex flex-col gap-8 py-12 md:py-16`}>
        <h2 id={id} className="text-2xl font-semibold tracking-tight text-balance">{title}</h2>
        {children}
      </div>
    </section>
  );
}

function Needs() {
  const t = useTranslations("product.needs");
  return (
    <Section id="product-needs" title={t("title")}>
      <ul className="grid gap-8 md:grid-cols-2 md:gap-x-12">
        {NEEDS.map((need) => (
          <li key={need.key} className="flex flex-col gap-3" data-testid={`need-${need.key}`}>
            <h3 className="text-lg font-semibold">{t(`rows.${need.key}.title`)}</h3>
            <Claim id={need.claim} className="text-base leading-7 text-muted-foreground" />
          </li>
        ))}
      </ul>
    </Section>
  );
}

const WORKFLOW = ["describe", "review", "follow"] as const;

function Workflow() {
  const t = useTranslations("product.workflow");
  return (
    <Section id="product-workflow" title={t("title")}>
      <div className="grid gap-8 lg:grid-cols-2 lg:gap-12">
        <div className="flex flex-col gap-4">
          <p className="max-w-prose text-base leading-7 text-muted-foreground">{t("description")}</p>
          <figure className="rounded-md border bg-muted p-5">
            <figcaption className="mb-3 text-sm font-medium text-muted-foreground">{t("exampleLabel")}</figcaption>
            <blockquote className="text-lg leading-7" data-testid="product-example">{t("example")}</blockquote>
          </figure>
        </div>
        <ol className="flex flex-col gap-6">
          {WORKFLOW.map((step, index) => (
            <li key={step} className="grid grid-cols-[auto_minmax(0,1fr)] gap-3">
              <span className="grid size-7 place-items-center rounded-full border text-sm tabular-nums" aria-hidden="true">{index + 1}</span>
              <div className="flex flex-col gap-1">
                <h3 className="text-base font-semibold">{t(`steps.${step}.title`)}</h3>
                <p className="text-base leading-7 text-muted-foreground">{t(`steps.${step}.body`)}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </Section>
  );
}

function GetStarted({ hubUrl }: Pick<Props, "hubUrl">) {
  const t = useTranslations("product.start");
  return (
    <Section id="product-start" title={t("title")}>
      <div className="grid gap-8 lg:grid-cols-2 lg:gap-12" data-testid="product-start">
        <div className="flex min-w-0 flex-col items-start gap-4">
          <h3 className="text-lg font-semibold">{t("team.title")}</h3>
          <Claim id="access" className="text-base leading-7" />
          <Button asChild variant="outline" className="min-h-11">
            <a href={INVITE_FORM_URL} data-testid="product-invite"><UserPlus aria-hidden="true" />{t("team.invite")}</a>
          </Button>
          <p className="text-xs text-muted-foreground">{t("team.inviteHint")}</p>
          <p className="text-base leading-7 text-muted-foreground">{t("team.prerequisites")}</p>
          <div className="flex w-full min-w-0 flex-col gap-2">
            <CommandLine command={INSTALL_COMMAND} label={t("team.installLabel")} />
            <CommandLine command={hubLoginCommand(hubUrl)} label={t("team.loginLabel")} testId="product-login-command" />
          </div>
          <p className="text-base leading-7">{t("team.next")}</p>
          <a href={docHref(GETTING_STARTED)} className={`${TEXT_LINK} inline-flex min-h-11 items-center gap-2`} data-testid="product-setup-guide">
            {t("team.guide")}<ArrowRight className="size-4 shrink-0" aria-hidden="true" />
          </a>
        </div>
        <div className="flex min-w-0 flex-col items-start gap-4 border-t pt-8 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-12">
          <h3 className="text-lg font-semibold">{t("local.title")}</h3>
          <p className="text-base leading-7">{t("local.body")}</p>
          <p className="text-base leading-7 text-muted-foreground">{t("local.prerequisites")}</p>
          <a href={docHref(LOCAL_GRAPH)} className={`${TEXT_LINK} inline-flex min-h-11 items-center gap-2`} data-testid="product-local-guide">
            {t("local.guide")}<ArrowRight className="size-4 shrink-0" aria-hidden="true" />
          </a>
          <Claim id="status" className="mt-4 text-sm leading-6 text-muted-foreground" />
        </div>
      </div>
    </Section>
  );
}

function Footer() {
  const t = useTranslations("product.footer");
  const links = [
    { href: docHref(GETTING_STARTED), label: t("docs") },
    { href: REPO_URL, label: t("source") },
    { href: docHref(ROADMAP), label: t("roadmap") },
    { href: docHref(LICENSE), label: t("license") },
  ];
  return (
    <footer className="border-t">
      <div className={`${COLUMN} flex flex-col gap-3 py-6 md:flex-row md:items-center md:justify-between`}>
        <span className="flex items-center gap-2 text-[13px] text-muted-foreground"><BrandMark className="size-5" />evo-agents</span>
        <nav aria-label={t("nav")}>
          <ul className="flex flex-wrap gap-x-5 text-[13px]">
            {links.map(({ href, label }) => (
              <li key={label}><a href={href} className={`${TEXT_LINK} inline-flex items-center max-md:min-h-11`}>{label}</a></li>
            ))}
          </ul>
        </nav>
      </div>
    </footer>
  );
}

/** A public introduction for new visitors. All screenshots are fixtures; no project data is loaded here. */
export function ProductPage({ signInHref, member, hubUrl }: Props) {
  const tApp = useTranslations("app");
  return (
    <div className="min-h-svh bg-background">
      <a href="#main" className="sr-only z-50 rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3">
        {tApp("skipToContent")}
      </a>
      <Header />
      <main id="main" data-testid="product-page">
        <Hero signInHref={signInHref} member={member} />
        <Needs />
        <Workflow />
        <GetStarted hubUrl={hubUrl} />
      </main>
      <Footer />
    </div>
  );
}
