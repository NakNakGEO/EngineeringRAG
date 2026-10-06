// Thin typed client. Everything the UI shows comes from the real API (proxied at /api).
export const BASE = "/api";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  init?: RequestInit & { adminToken?: string },
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (init?.adminToken) headers.set("X-EIOS-Admin-Token", init.adminToken);
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export interface EventEnvelope {
  event_id: string;
  run_id: string;
  seq: number;
  span_id: string;
  parent_span_id: string | null;
  type: string;
  actor_type: string;
  actor_id: string;
  status: string;
  summary: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Run {
  id: string;
  kind: string;
  goal: string;
  status: string;
  project_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface RunPage {
  items: Run[];
  next_cursor: string | null;
}

export interface EventPage {
  items: EventEnvelope[];
  next_cursor: number | null;
  has_more: boolean;
}

export interface GraphNode {
  span_id: string;
  parent_span_id: string | null;
  label: string;
  actor_type: string;
  actor_id: string;
  status: string;
  event_count: number;
}
export interface GraphEdge {
  source: string;
  target: string;
}
export interface RunGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}
