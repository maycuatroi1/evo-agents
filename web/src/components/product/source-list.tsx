import { useFormatter, useTranslations } from "next-intl";

import { TEXT_LINK } from "./claim";
import type { Source } from "./needs";

/** A day of needs.ts (ISO 8601) in the reader's language, the same on every server whatever its time zone. */
function useDay() {
  const format = useFormatter();
  return (day: string) => format.dateTime(new Date(`${day}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" });
}

function Details({ source }: { source: Source }) {
  const t = useTranslations("product.sources");
  const day = useDay();
  switch (source.kind) {
    case "issue":
      return (
        <>
          {t("issue", { repo: source.repo })}, {t("reactions", { count: source.reactions })},{" "}
          {source.closedOn ? t("closed", { date: day(source.closedOn) }) : t("open")}
        </>
      );
    case "hn":
      return (
        <>
          {t("hn")}, {t("discussion", { points: source.points, comments: source.comments })}
        </>
      );
    case "article":
      return <>{t("article", { publisher: source.publisher })}</>;
  }
}

/** Where people asked for what a row answers: each source's title as a link, what it is and its counts on `checkedOn`. */
export function SourceList({ sources, checkedOn }: { sources: Source[]; checkedOn: string }) {
  const t = useTranslations("product.sources");
  const day = useDay();
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-xs font-medium text-muted-foreground">{t("title")}</h4>
      <ul className="flex flex-col gap-2 text-[13px] leading-[18px]">
        {sources.map((source) => (
          <li key={source.url} className="[overflow-wrap:anywhere]">
            <a href={source.url} className={TEXT_LINK}>
              {source.title}
            </a>
            <span className="text-muted-foreground">
              {" "}
              (<Details source={source} />)
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">{t("asOf", { date: day(checkedOn) })}</p>
    </div>
  );
}
