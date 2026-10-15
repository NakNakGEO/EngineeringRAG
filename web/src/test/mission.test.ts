import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "../api";
import { deriveMission } from "../mission";

const ev = (seq: number, type: string, data: Record<string, unknown> = {}): EventEnvelope => ({
  event_id: `e${seq}`, run_id: "r", seq, span_id: "s", parent_span_id: null, type,
  actor_type: "system", actor_id: "team_selector", status: "completed", summary: "", data,
  timestamp: new Date(1_700_000_000_000 + seq * 1000).toISOString(),
});

const picked = (seq: number, role: string, writer?: boolean) =>
  ev(seq, "AGENT_SELECTED", writer === undefined ? { role } : { role, writer });

describe("deriveMission active agent", () => {
  it("shows the writer, not the last reviewer selected", () => {
    const m = deriveMission([
      picked(1, "software_engineer", true),
      picked(2, "architect", false),
      picked(3, "qa_engineer", false),
    ]);
    expect(m.activeAgent).toBe("software_engineer");
    expect(m.reviewers).toEqual(["architect", "qa_engineer"]);
  });

  it("does not depend on the order the roles were selected in", () => {
    const m = deriveMission([picked(1, "code_reviewer", false), picked(2, "software_engineer", true)]);
    expect(m.activeAgent).toBe("software_engineer");
    expect(m.reviewers).toEqual(["code_reviewer"]);
  });

  it("lists nothing as reviewers when the writer is the only role", () => {
    const m = deriveMission([picked(1, "technical_writer", true)]);
    expect(m).toMatchObject({ activeAgent: "technical_writer", reviewers: [] });
  });

  it("counts a role selected twice once", () => {
    const m = deriveMission([picked(1, "software_engineer", true), picked(2, "architect", false), picked(3, "architect", false)]);
    expect(m.reviewers).toEqual(["architect"]);
  });

  it("falls back to the last role for events that do not say who writes", () => {
    const m = deriveMission([picked(1, "planner"), picked(2, "executor")]);
    expect(m.activeAgent).toBe("executor");
    expect(m.reviewers).toEqual(["planner"]);
  });

  it("ignores selections without a role and is empty without any", () => {
    expect(deriveMission([ev(1, "AGENT_SELECTED", { writer: true })])).toMatchObject({ activeAgent: null, reviewers: [] });
    expect(deriveMission([])).toMatchObject({ activeAgent: null, reviewers: [], outcome: "running" });
  });
});
