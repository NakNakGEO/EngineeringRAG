import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../api";
import { deriveMission } from "../mission";
import { initialStream, streamReducer } from "../stream";

const ev = (seq: number, type = "TOOL_STARTED", data: Record<string, unknown> = {}): EventEnvelope => ({
  event_id: `e${seq}`, run_id: "r", seq, span_id: "s", parent_span_id: null, type,
  actor_type: "tool", actor_id: "x", status: "completed", summary: "", data,
  timestamp: new Date(1_700_000_000_000 + seq * 1000).toISOString(),
});

describe("streamReducer", () => {
  it("orders, de-duplicates and resumes", () => {
    let s = initialStream();
    s = streamReducer(s, { kind: "events", items: [ev(2), ev(1), ev(3)], limit: 100 });
    expect(s.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    s = streamReducer(s, { kind: "events", items: [ev(2), ev(3), ev(4)], limit: 100 });
    expect(s.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(s.lastSeq).toBe(4);
  });

  it("keeps memory bounded and counts what it dropped", () => {
    let s = initialStream();
    for (let i = 1; i <= 50; i += 10) {
      s = streamReducer(s, { kind: "events", items: Array.from({ length: 10 }, (_, j) => ev(i + j)), limit: 20 });
    }
    expect(s.events).toHaveLength(20);
    expect(s.events[0]!.seq).toBe(31);
    expect(s.dropped).toBe(30);
    expect(s.lastSeq).toBe(50);
  });

  it("ignores empty or stale batches without changing identity", () => {
    const s = streamReducer(initialStream(), { kind: "events", items: [ev(5)], limit: 10 });
    expect(streamReducer(s, { kind: "events", items: [ev(5), ev(4)], limit: 10 })).toBe(s);
  });
});

describe("deriveMission", () => {
  it("is computed only from events", () => {
    const m = deriveMission([
      ev(1, "RUN_STARTED", { goal: "fix retries" }),
      ev(2, "WORKFLOW_NODE_STARTED", { stage: "UNDERSTAND", node: "understand" }),
      ev(3, "TOOL_SELECTED", { selected: { provider: "sql_analyze" } }),
      ev(4, "POLICY_DENIED", { effect: "deny", rule_id: "root.external_database" }),
      ev(5, "LLM_COMPLETED", { input_tokens: 10, output_tokens: 5 }),
      ev(6, "RUN_FAILED"),
    ]);
    expect(m).toMatchObject({
      goal: "fix retries", workflowNode: "UNDERSTAND/understand", provider: "sql_analyze",
      tokens: 15, llmCalls: 1, outcome: "failed",
      policy: { effect: "deny", rule: "root.external_database" },
    });
    expect(m.durationMs).toBe(5000);
  });
});
