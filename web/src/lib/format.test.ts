import { describe, expect, it } from "vitest";
import { formatBRL, formatPercent } from "./format";

describe("formatBRL", () => {
  it("formats centavos with thousands separator and comma decimals", () => {
    expect(formatBRL(123456)).toBe("R$ 1.234,56");
  });

  it("pads single-digit cents", () => {
    expect(formatBRL(100005)).toBe("R$ 1.000,05");
  });

  it("handles negative centavos (write-offs, refunds)", () => {
    expect(formatBRL(-4200)).toBe("-R$ 42,00");
  });

  it("handles zero", () => {
    expect(formatBRL(0)).toBe("R$ 0,00");
  });
});

describe("formatPercent", () => {
  it("formats a ratio to one decimal by default", () => {
    expect(formatPercent(0.029)).toBe("2.9%");
  });

  it("supports tighter precision for exact-ratio detection (fault E, 2.500%)", () => {
    expect(formatPercent(0.025, 3)).toBe("2.500%");
  });
});
