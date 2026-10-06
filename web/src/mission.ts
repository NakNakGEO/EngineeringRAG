import type { EventEnvelope } from "./api";

/** What Mission Control shows, derived purely from the event stream (no fake data). */
export interface Mission {
  goal: string;
  workflowNode: string | null;
  activeAgent: string | null;
  skill: string | null;
  capability: string | null;
  provider: string | null;
  policy: { effect: string; rule: string } | null;
  confidence: number | null;
  readyToAct: boolean | null;
  tokens: number;
  llmCalls: number;
  toolCalls: number;
  retries: number;
  build: string | null;
  test: string | null;
  outcome: "running" | "completed" | "failed";
  durationMs: number | null;
  pendingApproval: string | null;
}

const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

export function deriveMission(events: EventEnvelope[]): Mission {
  const m: Mission = {
    goal: "", workflowNode: null, activeAgent: null, skill: null, capability: null,
    provider: null, policy: null, confidence: null, readyToAct: null, tokens: 0,
    llmCalls: 0, toolCalls: 0, retries: 0, build: null, test: null, outcome: "running",
    durationMs: null, pendingApproval: null,
  };
  for (const e of events) {
    const d = e.data;
    switch (e.type) {
      case "RUN_STARTED": m.goal = str(d.goal) ?? m.goal; break;
      case "WORKFLOW_NODE_STARTED": m.workflowNode = `${str(d.stage) ?? ""}/${str(d.node) ?? ""}`; break;
      case "AGENT_SELECTED": m.activeAgent = str(d.role); break;
      case "SKILL_SELECTED": m.skill = str(d.skill) ?? e.summary; break;
      case "CAPABILITY_REQUESTED": m.capability = str(d.capability); break;
      case "TOOL_SELECTED": m.provider = str((d.selected as Record<string, unknown> | null)?.provider); break;
      case "POLICY_ALLOWED":
      case "POLICY_DENIED":
        m.policy = { effect: str(d.effect) ?? e.type, rule: str(d.rule_id) ?? "" };
        break;
      case "APPROVAL_REQUESTED": m.pendingApproval = str(d.approval_id); break;
      case "APPROVAL_DECIDED": m.pendingApproval = null; break;
      case "CONTEXT_EXPANDED":
      case "CONTEXT_REQUESTED":
        if (typeof d.confidence === "number") m.confidence = d.confidence;
        if (typeof d.ready_to_act === "boolean") m.readyToAct = d.ready_to_act;
        break;
      case "LLM_COMPLETED":
        m.llmCalls += 1;
        m.tokens += num(d.input_tokens) + num(d.output_tokens);
        break;
      case "TOOL_STARTED": m.toolCalls += 1; break;
      case "RETRY_STARTED": m.retries += 1; break;
      case "BUILD_COMPLETED": m.build = e.status; break;
      case "TEST_COMPLETED": m.test = e.status; break;
      case "RUN_COMPLETED": m.outcome = "completed"; break;
      case "RUN_FAILED": m.outcome = "failed"; break;
    }
  }
  const first = events[0], last = events[events.length - 1];
  if (first && last) m.durationMs = Date.parse(last.timestamp) - Date.parse(first.timestamp);
  return m;
}
