"use client";

import { ArrowLeftRight, Trash2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { fits, MAX_DESCRIPTION_CHARS, type SpecEdge, type SystemProblem, withEdge } from "@/lib/systems";

import { ProblemList } from "./problem-list";
import type { Designer } from "./use-designer";

type Props = {
  edge: SpecEdge;
  index: number;
  problems: readonly SystemProblem[];
  designer: Designer;
  canChange: boolean;
};

/**
 * The side panel of a selected edge: what it means (its kind, from which node to which), what the hub finds wrong
 * with it, its description, which the agents at its ends read (written into the spec when the field loses the focus,
 * one undo step), Reverse when the kinds take it the other way, and Remove.
 */
export function EdgePanel({ edge, index, problems, designer, canChange }: Props) {
  const t = useTranslations("systems.designer");
  const format = useFormatter();
  const { spec, change, select } = designer;
  const [text, setText] = useState(edge.description);
  const kindOf = (id: string) => spec.nodes.find((node) => node.id === id)?.kind;
  const from = kindOf(edge.from);
  const to = kindOf(edge.to);
  const reversible =
    from !== undefined &&
    to !== undefined &&
    fits(edge.kind, to, from) &&
    !spec.edges.some((other) => other.from === edge.to && other.to === edge.from && other.kind === edge.kind);
  const nameOf = (id: string) => {
    const node = spec.nodes.find((one) => one.id === id);
    return node?.label || (node?.profile ? `@${node.profile}` : node?.kind === "kg" ? (node.graph ?? id) : id);
  };
  const commit = () => {
    if (text.trim() !== edge.description.trim()) change(withEdge(spec, index, { description: text.trim() }));
  };
  const long = [...text].length > MAX_DESCRIPTION_CHARS;

  return (
    <div className="flex flex-col gap-5" data-testid="system-edge-panel" data-edge={`e${index}`}>
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold">{t(`edgeKinds.${edge.kind}`)}</h2>
        <p className="text-sm text-muted-foreground">{t("edgeLine", { from: nameOf(edge.from), kind: t(`edgeKinds.${edge.kind}`), to: nameOf(edge.to) })}</p>
        <p className="text-xs text-pretty text-muted-foreground">{t(`edgeHint.${edge.kind}`)}</p>
      </div>
      <ProblemList problems={problems} />
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        {t("description")}
        <Textarea
          value={text}
          rows={4}
          onChange={(event) => setText(event.target.value)}
          onBlur={commit}
          disabled={!canChange}
          aria-invalid={long || undefined}
          className="max-h-48 font-normal"
          data-testid="system-edge-description"
        />
        <span className={long ? "text-xs font-normal text-danger" : "text-xs font-normal text-muted-foreground"}>
          {t("descriptionHint", { count: format.number([...text].length), max: format.number(MAX_DESCRIPTION_CHARS) })}
        </span>
      </label>
      {canChange ? (
        <div className="flex flex-wrap gap-2">
          {reversible ? (
            <Button type="button" variant="outline" size="sm" onClick={() => change(withEdge(spec, index, { from: edge.to, to: edge.from }))} data-testid="system-edge-reverse">
              <ArrowLeftRight aria-hidden="true" />
              {t("reverse")}
            </Button>
          ) : null}
          <Button
            type="button"
            variant="quiet-danger"
            size="sm"
            onClick={() => {
              change(withEdge(spec, index, null));
              select(null);
            }}
            data-testid="system-edge-remove"
          >
            <Trash2 aria-hidden="true" />
            {t("removeEdge")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
