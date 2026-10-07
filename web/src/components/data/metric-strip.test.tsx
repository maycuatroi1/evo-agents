// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { CircleCheck, CircleX, Clock, Eye } from "lucide-react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { type Metric, MetricStrip, QuietLine } from "./metric-strip";

beforeAll(() => {
  // Recharts measures its container; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

const METRICS: Metric[] = [
  { id: "running", label: "Đang chạy", icon: CircleCheck, live: true, tone: "running", inFlight: true, value: 1, meta: "#13 trên M1s-Mac-mini" },
  { id: "queued", label: "Đang chờ", icon: Clock, inFlight: true, value: 0, meta: "không có run nào chờ" },
  { id: "review", label: "Chờ duyệt", icon: Eye, tone: "attention", inFlight: true, value: 2 },
  { id: "failed", label: "Thất bại", icon: CircleX, value: 1, extra: "và 1 run mất lease" },
];

describe("MetricStrip", () => {
  it("puts the counts in one list, a zero quiet, the running and attention cells in their tone", () => {
    renderVi(<MetricStrip label="Tóm tắt" metrics={METRICS} testId="strip" />);
    const strip = screen.getByRole("region", { name: "Tóm tắt" });
    expect(strip).toHaveAttribute("data-testid", "strip");
    // One container: a single list whose cells are divided by the 1 px gap over the border colour.
    const lists = strip.querySelectorAll("dl");
    expect(lists).toHaveLength(1);
    expect(lists[0].className).toContain("gap-px");
    expect(within(strip).getAllByRole("term")).toHaveLength(4);
    expect(screen.getByTestId("summary-running-value")).toHaveTextContent("1");
    expect(screen.getByTestId("summary-running-value").className).toContain("text-running");
    expect(screen.getByTestId("summary-queued-value").className).toContain("text-fg-subtle");
    expect(screen.getByTestId("summary-review-value").className).toContain("text-attention");
    expect(screen.getByTestId("summary-failed-value")).toHaveTextContent("1và 1 run mất lease");
    expect(screen.getByTestId("summary-running")).toHaveTextContent("#13 trên M1s-Mac-mini");
    // The running cell pulses only while something runs.
    expect(screen.getByTestId("summary-running").querySelector(".animate-live-ping")).not.toBeNull();
  });

  it("gives way to one quiet line when every in-flight count is zero", () => {
    const calm = METRICS.map((metric) => (metric.inFlight ? { ...metric, value: 0 } : metric));
    renderVi(
      <MetricStrip
        label="Tóm tắt"
        metrics={calm}
        testId="strip"
        quiet={
          <QuietLine icon={CircleCheck} title="Mọi thứ yên ắng." testId="quiet">
            Không run nào đang chạy.
          </QuietLine>
        }
      />,
    );
    expect(screen.getByTestId("strip")).toHaveAttribute("data-quiet", "true");
    expect(screen.getByTestId("quiet")).toHaveTextContent("Mọi thứ yên ắng. Không run nào đang chạy.");
    expect(screen.queryByTestId("summary-failed")).toBeNull();
  });

  it("keeps the strip without a quiet line, even when every count is zero", () => {
    renderVi(<MetricStrip label="Tóm tắt" metrics={METRICS.map((metric) => ({ ...metric, value: 0 }))} />);
    expect(screen.getByTestId("summary-running").querySelector(".animate-live-ping")).toBeNull();
    expect(screen.getAllByRole("definition").length).toBeGreaterThan(0);
  });

  it("names every value of a sparkline in words while the chart loads on demand", async () => {
    const label = "Run xong mỗi ngày, 1 đến 7 tháng 10: 0, 0, 1, 0, 2, 0, 5";
    renderVi(
      <MetricStrip
        label="Tóm tắt"
        metrics={[{ id: "done", label: "Xong, 7 ngày", icon: CircleCheck, value: 8, series: { values: [0, 0, 1, 0, 2, 0, 5], label } }]}
      />,
    );
    const image = screen.getByRole("img", { name: label });
    // The chart is drawn for the eye only and takes no focus; the label carries the values.
    expect(image.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(await screen.findByTestId("sparkline-chart")).toBeInTheDocument();
    expect(image.querySelector("[tabindex]")).toBeNull();
  });
});
