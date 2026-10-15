import { describe, expect, it } from "vitest";
import { fromCode, fromKnowledge, location, vaultState, type CodeItem } from "../knowledge";

const candidate = (over: Partial<CodeItem["candidate"]> = {}): CodeItem["candidate"] => ({
  id: "symbol:a.py::A", kind: "symbol", title: "A (class)", content: "class A", path: "a.py",
  start_line: 3, end_line: 9, trust: null, health: null, confidence: null, scope: "committed", ...over,
});

describe("vaultState", () => {
  it("is empty when no indexed file has linked knowledge", () => {
    const s = vaultState([{ files_total: 381, files_with_knowledge: 0, knowledge_coverage: 0 }]);
    expect(s).toEqual({ projects: 1, files: 381, linked: 0, empty: true });
  });

  it("is not empty once any project has linked knowledge", () => {
    const s = vaultState([
      { files_total: 10, files_with_knowledge: 0 },
      { files_total: 5, files_with_knowledge: 2 },
    ]);
    expect(s).toEqual({ projects: 2, files: 15, linked: 2, empty: false });
  });

  it("is unknown (null) while coverage is loading, missing or malformed", () => {
    expect(vaultState([])).toBeNull();
    expect(vaultState([undefined])).toBeNull();
    expect(vaultState([{ files_total: 3 }, { files_with_knowledge: "0" }])).toBeNull();
  });

  it("ignores projects whose coverage is still loading", () => {
    expect(vaultState([undefined, { files_total: 4, files_with_knowledge: 1 }])).toMatchObject({ projects: 1, empty: false });
  });
});

describe("search result view models", () => {
  it("maps a knowledge hit", () => {
    const r = fromKnowledge({
      item: { id: "k1", title: "T", kind: "decision", trust: "TRUSTED", health: "CURRENT", vault: "project", content: "body", confidence: 0.9 },
      score: 0.5,
    });
    expect(r).toMatchObject({ id: "k1", title: "T", trust: "TRUSTED", where: "project", confidence: 0.9, body: "body", sources: [] });
  });

  it("maps a code result with its location and sources", () => {
    const r = fromCode({ candidate: candidate(), score: 0.07, sources: ["exact", "symbol"], reasons: [] });
    expect(r).toMatchObject({ id: "symbol:a.py::A", kind: "symbol", trust: null, where: "a.py:3-9", sources: ["exact", "symbol"] });
  });

  it("formats locations without line numbers, single lines and path-less items", () => {
    expect(location(candidate({ start_line: null, end_line: null }))).toBe("a.py");
    expect(location(candidate({ start_line: 7, end_line: 7 }))).toBe("a.py:7");
    expect(location(candidate({ path: null, scope: "overlay" }))).toBe("overlay");
    expect(location(candidate({ path: null, scope: null }))).toBe("");
  });
});
