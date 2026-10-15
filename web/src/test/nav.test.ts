import { describe, expect, it } from "vitest";
import type { Relation } from "../data";
import { describeConnection, friendly } from "../graph/connection";
import { canGoBack, canGoForward, currentEntry, EMPTY_NAV, navBack, navForward, navGo, previousEntry } from "../graph/nav";

describe("navigation trail", () => {
  it("goes back and forward through what was picked", () => {
    let n = navGo(EMPTY_NAV, "a", "map");
    n = navGo(n, "b", "link");
    n = navGo(n, "c", "link");
    expect(currentEntry(n)?.id).toBe("c");
    expect(previousEntry(n)?.id).toBe("b");
    n = navBack(n);
    expect(currentEntry(n)?.id).toBe("b");
    n = navBack(n);
    expect(currentEntry(n)?.id).toBe("a");
    expect(canGoBack(n)).toBe(false);
    expect(canGoForward(n)).toBe(true);
    n = navForward(navForward(n));
    expect(currentEntry(n)?.id).toBe("c");
  });

  it("picking from the middle drops what was ahead", () => {
    let n = navGo(navGo(navGo(EMPTY_NAV, "a", "map"), "b", "link"), "c", "link");
    n = navBack(navBack(n));
    n = navGo(n, "x", "map");
    expect(n.list.map((e) => e.id)).toEqual(["a", "x"]);
    expect(canGoForward(n)).toBe(false);
  });

  it("picking the file that is already open changes nothing", () => {
    const n = navGo(EMPTY_NAV, "a", "map");
    expect(navGo(n, "a", "link")).toBe(n);
  });

  it("remembers how each file was reached", () => {
    const n = navGo(navGo(EMPTY_NAV, "a", "search"), "b", "link");
    expect(currentEntry(n)?.via).toBe("link");
    expect(previousEntry(n)?.via).toBe("search");
  });

  it("skips files that no longer exist when going back", () => {
    const n = navGo(navGo(navGo(EMPTY_NAV, "a", "map"), "gone", "link"), "c", "link");
    const valid = (id: string) => id !== "gone";
    expect(currentEntry(navBack(n, valid))?.id).toBe("a");
  });

  it("is bounded", () => {
    let n = EMPTY_NAV;
    for (let i = 0; i < 200; i++) n = navGo(n, `f${i}`, "link");
    expect(n.list.length).toBeLessThanOrEqual(60);
    expect(currentEntry(n)?.id).toBe("f199");
  });
});

const rel = (over: Partial<Relation>): Relation => ({
  direction: "out", kind: "imports", confidence: 1, own: "", line: null, names: [],
  other: { key: "k", kind: "file", label: "x.py", path: "a/x.py" }, ...over,
});

describe("how two files connect", () => {
  it("says who imports whom, and ignores relations to other files", () => {
    const rels = [
      rel({ direction: "out", other: { key: "1", kind: "file", label: "x.py", path: "a/x.py" } }),
      rel({ direction: "in", other: { key: "2", kind: "file", label: "y.py", path: "b/y.py" } }),
    ];
    expect(describeConnection(rels, "c/me.py", "a/x.py").map((l) => l.text)).toEqual(["me.py imports x.py"]);
    expect(describeConnection(rels, "c/me.py", "b/y.py").map((l) => l.text)).toEqual(["y.py imports me.py"]);
  });

  it("names the functions that are called, with the line, imports first", () => {
    const rels = [
      rel({ kind: "calls", direction: "out", names: ["load_candles"], line: 40, other: { key: "1", kind: "function", label: "load_candles", path: "a/x.py" } }),
      rel({ kind: "imports", direction: "out", line: 3 }),
      rel({ kind: "calls", direction: "in", own: "run", names: ["run"], other: { key: "2", kind: "function", label: "run", path: "a/x.py" } }),
    ];
    const out = describeConnection(rels, "c/me.py", "a/x.py");
    expect(out.map((l) => l.text)).toEqual(["me.py imports x.py", "me.py calls load_candles in x.py", "x.py calls run in me.py"]);
    expect(out[1]!.line).toBe(40);
  });

  it("never shows raw keys: a callee is named, not 'symbol:path::name'", () => {
    expect(friendly("symbol:trading/src/engine/candle_store.py::CandleStore.append")).toBe("CandleStore.append");
    expect(friendly("file:trading/scripts/run.py")).toBe("run.py");
    const rels = [rel({
      kind: "calls", direction: "in", own: "symbol:c/me.py::CandleStore.read", names: [],
      other: { key: "symbol:a/x.py::aggregate", kind: "function", label: "aggregate", path: "a/x.py" },
    })];
    const t = describeConnection(rels, "c/me.py", "a/x.py")[0]!.text;
    expect(t).toBe("x.py calls CandleStore.read in me.py");
    expect(t).not.toContain("symbol:");
  });

  it("does not repeat the same sentence", () => {
    const r = rel({});
    expect(describeConnection([r, r, r], "c/me.py", "a/x.py")).toHaveLength(1);
  });
});
