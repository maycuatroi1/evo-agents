"use client";

import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { SafeMarkdown } from "@/components/data/markdown";

import type { SrsItem } from "./api";
import { CodeChip } from "./badges";
import {
  proseOutsideTables,
  rulesOf,
  screensOf,
  sectionMarkdown,
  splitCodes,
  stepRefs,
  stepsOf,
  parentUseCase,
  factsOf,
  screenUsers,
} from "./content-model";
import { childrenOf, CODED_KINDS, isRow } from "./model";

/** Where an item's codes lead (the item's page) and where a heading without a code opens (the document, at it). */
export type ItemLinks = { item: (ref: string) => Route; section: (key: string) => Route };

type Props = { items: SrsItem[]; item: SrsItem; links: ItemLinks };

const H3 = "mt-6 mb-2 text-[15px] leading-[22px] font-semibold first:mt-0";

/** A line of text with each code it names as a link to that item. */
export function CodeText({ text, items, links }: { text: string; items: SrsItem[]; links: ItemLinks }) {
  return (
    <>
      {splitCodes(text, items).map((part, index) =>
        typeof part === "string" ? (
          <span key={index}>{part}</span>
        ) : (
          <Link key={index} href={links.item(part.ref)} className="font-mono text-[0.92em] whitespace-nowrap text-brand underline decoration-brand/40 underline-offset-2 hover:text-brand-hover">
            {part.code}
          </Link>
        ),
      )}
    </>
  );
}

function fieldName(name: string): string {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Facts({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[minmax(7rem,auto)_1fr]" data-testid="srs-facts">
      {rows.map((row) => (
        <div key={row.label} className="contents">
          <dt className="text-[13px] text-muted-foreground max-sm:mt-1">{row.label}</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere]">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Rule({ rule, links }: { rule: SrsItem; links: ItemLinks }) {
  const t = useTranslations("srs.item");
  const steps = stepRefs(rule);
  return (
    <article className="rounded-md border p-4" aria-labelledby={`rule-${rule.key}`} data-testid="srs-rule">
      <header className="mb-2 flex flex-wrap items-center gap-2">
        {steps.length ? <span className="text-xs text-muted-foreground">{t("atSteps", { steps: steps.join(", "), count: steps.length })}</span> : null}
        {rule.code ? <CodeChip code={rule.code} href={links.item(rule.code)} /> : null}
        <h4 id={`rule-${rule.key}`} className="text-sm font-medium">
          {rule.title}
        </h4>
      </header>
      {rule.body.trim() ? <SafeMarkdown testId="srs-markdown">{rule.body}</SafeMarkdown> : null}
    </article>
  );
}

function UseCase({ items, item, links }: Props) {
  const t = useTranslations("srs.item");
  const tField = useTranslations("srs.field");
  const facts = factsOf(item);
  const steps = stepsOf(item);
  const prose = proseOutsideTables(item.body);
  const rules = rulesOf(items, item);
  const screens = screensOf(items, item);
  const ids = [item.code, ...item.aliases].filter(Boolean).join(", ");
  return (
    <div className="flex flex-col" data-testid="srs-use-case">
      <Facts
        rows={[
          ...(ids ? [{ label: tField("codes"), value: <span className="font-mono text-[13px]">{ids}</span> }] : []),
          ...facts
            .filter((fact) => fact.name !== "id")
            .map((fact) => ({
              label: fact.known ? tField(fact.name as never) : fieldName(fact.name),
              value: <CodeText text={fact.value} items={items} links={links} />,
            })),
        ]}
      />
      <h3 className={H3}>{t("activityFlow")}</h3>
      {steps.rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("noSteps")}</p>
      ) : (
        <div role="region" aria-label={t("stepsLabel", { code: item.code ?? item.title })} tabIndex={0} className="overflow-x-auto rounded-md border">
          <table className="w-full border-collapse text-sm" data-testid="srs-steps">
            <caption className="sr-only">{t("stepsLabel", { code: item.code ?? item.title })}</caption>
            <thead>
              <tr>
                {steps.columns.map((column) => (
                  <th key={column} scope="col" className="border-b bg-muted/50 px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                    {fieldName(column)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {steps.rows.map((row, index) => (
                <tr key={index}>
                  {steps.columns.map((column, at) => (
                    <td key={column} className={at === 0 ? "border-b px-3 py-2 align-top font-mono text-[13px]" : "border-b px-3 py-2 align-top"}>
                      {row[column] ? <CodeText text={row[column]} items={items} links={links} /> : <span className="text-fg-subtle">-</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {prose ? (
        <div className="mt-4">
          <SafeMarkdown testId="srs-markdown">{prose}</SafeMarkdown>
        </div>
      ) : null}
      <h3 className={H3}>{t("rules")}</h3>
      {rules.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("noRules")}</p>
      ) : (
        <div className="flex flex-col gap-3">
          {rules.map((rule) => (
            <Rule key={rule.key} rule={rule} links={links} />
          ))}
        </div>
      )}
      <h3 className={H3}>{t("screens")}</h3>
      {screens.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("noScreens")}</p>
      ) : (
        <ul className="flex flex-wrap gap-2" role="list" data-testid="srs-screens">
          {screens.map(({ screen, linked }) => (
            <li key={screen.key} className="inline-flex items-center gap-1.5 text-[13px]">
              {screen.code ? <CodeChip code={screen.code} href={links.item(screen.code)} /> : null}
              <span>{screen.title}</span>
              {linked ? null : <span className="text-xs text-fg-subtle">{t("namedInText")}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A table row (an NFR, a common rule): its cells under the headers of its section's table. */
function Row({ items, item }: { items: SrsItem[]; item: SrsItem }) {
  const parent = items.find((one) => one.key === item.parent);
  const header = (parent?.body ?? "").split("\n").find((line) => line.trim().startsWith("|")) ?? "";
  const heads = header.split("|").slice(1, -1).map((cell) => cell.trim());
  const cells = Array.isArray(item.fields.cells) ? item.fields.cells.map(String) : [item.body];
  return <Facts rows={cells.map((cell, index) => ({ label: heads[index] || String(index + 1), value: cell }))} />;
}

/**
 * An item of a version as the page reads it: a use case's fields, Activity Flow, business rules and screens, a
 * business rule with the use case it belongs to, a screen with the use cases that use it, a table row as its cells,
 * and any other heading as its Markdown with the items under it.
 */
export function ItemBody({ items, item, links }: Props) {
  const t = useTranslations("srs.item");
  if (item.kind === "use_case") return <UseCase items={items} item={item} links={links} />;
  if (isRow(item)) return <Row items={items} item={item} />;
  const markdown = sectionMarkdown(items, item);
  const above = item.kind === "business_rule" ? parentUseCase(items, item) : null;
  const users = item.kind === "screen" ? screenUsers(items, item) : [];
  const below = childrenOf(items, item.key);
  const steps = stepRefs(item);
  return (
    <div className="flex flex-col gap-4" data-testid="srs-item-body">
      {above ? (
        <p className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
          {t("belongsTo")}
          {above.code ? <CodeChip code={above.code} href={links.item(above.code)} /> : <span>{above.title}</span>}
          {steps.length ? <span>{t("atSteps", { steps: steps.join(", "), count: steps.length })}</span> : null}
        </p>
      ) : null}
      {markdown ? <SafeMarkdown testId="srs-markdown">{markdown}</SafeMarkdown> : <p className="text-[13px] text-fg-subtle">{t("empty")}</p>}
      {users.length ? (
        <p className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground">
          {t("usedBy")}
          {users.map((user) => (user.code ? <CodeChip key={user.key} code={user.code} href={links.item(user.code)} /> : null))}
        </p>
      ) : null}
      {below.length ? (
        <div>
          <h3 className={H3}>{t("inSection")}</h3>
          <ul className="flex flex-col gap-1.5" role="list" data-testid="srs-children">
            {below.map((child) => (
              <li key={child.key} className="flex min-w-0 items-center gap-2 text-sm">
                {child.code && CODED_KINDS.has(child.kind) ? <CodeChip code={child.code} href={links.item(child.code)} /> : null}
                <Link href={links.section(child.key)} className="min-w-0 text-foreground [overflow-wrap:anywhere] hover:text-brand">
                  {child.title}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
