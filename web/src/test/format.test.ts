import { describe, expect, it } from "vitest";
import { formatMetric } from "../format";

describe("formatMetric", () => {
  it("rounds fractions to two decimals", () => {
    expect(formatMetric("symbol_coverage", 0.8939929328621908)).toBe("0.89");
    expect(formatMetric("anything", 0.5)).toBe("0.50");
  });

  it("keeps whole fractions readable as fractions", () => {
    expect(formatMetric("knowledge_coverage", 0)).toBe("0.00");
    expect(formatMetric("symbol_coverage", 1)).toBe("1.00");
  });

  it("leaves counts alone", () => {
    expect(formatMetric("files_total", 381)).toBe("381");
    expect(formatMetric("parse_errors", 0)).toBe("0");
  });

  it("passes text through and shows a dash for missing or non-finite values", () => {
    expect(formatMetric("branch", "main")).toBe("main");
    expect(formatMetric("flag", true)).toBe("true");
    expect(formatMetric("x", null)).toBe("—");
    expect(formatMetric("x", undefined)).toBe("—");
    expect(formatMetric("x", Number.NaN)).toBe("—");
  });
});
