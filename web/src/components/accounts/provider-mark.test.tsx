// @vitest-environment jsdom
import { fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { ProviderMark } from "./provider-mark";

const GLM = { color: "#2563EB", text_icon: "GL", name: "GLM Coding" };

describe("the mark of a provider", () => {
  it("draws the provider's logo from public/provider-logos on a white tile, decorative", () => {
    const { container } = renderVi(<ProviderMark id="glm" entry={GLM} size="sm" />);
    const mark = container.querySelector('[data-provider-mark="glm"]');
    expect(mark).toHaveAttribute("aria-hidden", "true");
    expect(mark).toHaveClass("bg-white");
    const logo = mark?.querySelector("img");
    expect(logo).toHaveAttribute("src", "/provider-logos/glm.svg");
    expect(logo).toHaveAttribute("alt", "");
    expect(logo).toHaveAttribute("loading", "lazy");
    expect(mark).not.toHaveTextContent("GL");
  });

  it("shares one file between providers of one brand", () => {
    const { container } = renderVi(
      <>
        <ProviderMark id="ollama-local" entry={null} />
        <ProviderMark id="ollama-cloud" entry={null} />
      </>,
    );
    const files = [...container.querySelectorAll("img")].map((img) => img.getAttribute("src"));
    expect(files).toEqual(["/provider-logos/ollama-local.svg", "/provider-logos/ollama-local.svg"]);
  });

  it("falls back to the catalog's letters on its colour when the provider has no logo or the logo fails", () => {
    const { container } = renderVi(
      <>
        <ProviderMark id="custom-openai" entry={{ color: "#64748B", text_icon: "OA", name: "Custom" }} />
        <ProviderMark id="glm" entry={GLM} />
      </>,
    );
    const custom = container.querySelector('[data-provider-mark="custom-openai"]');
    expect(custom).toHaveTextContent("OA");
    expect(custom?.querySelector("img")).toBeNull();
    const glm = container.querySelector('[data-provider-mark="glm"]');
    fireEvent.error(glm!.querySelector("img")!);
    expect(container.querySelector('[data-provider-mark="glm"]')).toHaveTextContent("GL");
    expect(container.querySelector('[data-provider-mark="glm"] img')).toBeNull();
  });
});
