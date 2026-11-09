// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { FieldText } from "./field-diff";
import type { ItemLinks } from "./item-body";

const LINKS: ItemLinks = { item: (ref) => `/items/${ref}` as never, section: (key) => `/sections/${key}` as never };

const OLD = "The shopper picks the items, the address and the payment, and the shop holds the stock until it is paid. ".repeat(3);
const NEW = "Northwind Billing sends the invoice once the payment clears, then the warehouse ships the parcel and tells the buyer. ".repeat(3);

describe("FieldText", () => {
  it("shows a long rewrite as its new text, with the text in use and each change a click away", async () => {
    renderVi(<FieldText row={{ key: "context", before: OLD, after: NEW, change: "changed" }} items={[]} links={LINKS} />);
    const field = screen.getByTestId("srs-case-field-rewritten");
    expect(field).toHaveTextContent("Đoạn này được viết lại nhiều, dưới đây là bản mới.");
    expect(field).toHaveTextContent("Northwind Billing sends the invoice");
    expect(field.querySelector("ins, del")).toBeNull();
    await userEvent.click(within(field).getByRole("button", { name: "Xem bản đang dùng" }));
    expect(screen.getByTestId("srs-case-field-old")).toHaveTextContent("The shopper picks the items");
    await userEvent.click(within(field).getByRole("button", { name: "Xem từng chỗ đổi" }));
    expect(screen.queryByTestId("srs-case-field-old")).toBeNull();
    expect(field.querySelector("ins")).not.toBeNull();
    await userEvent.click(within(field).getByRole("button", { name: "Xem bản mới" }));
    expect(field.querySelector("ins, del")).toBeNull();
  });
});
