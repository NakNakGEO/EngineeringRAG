import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, type RunGraph } from "../api";
import { Empty, ErrorNote, Panel } from "../ui";
import { RunPicker } from "./Mission";

const COLORS: Record<string, string> = { completed: "#2e7d32", failed: "#c62828", denied: "#ef6c00", started: "#1565c0" };

/** Spans laid out by depth. Large runs are collapsed: children beyond MAX_VISIBLE stay folded
 * until the user expands that span (incremental graph expansion; the API caps the graph too). */
const MAX_VISIBLE = 60;

export function Graph({ runId, onRun }: { runId: string | null; onRun: (id: string) => void }) {
  const graph = useQuery({
    queryKey: ["graph", runId],
    enabled: !!runId,
    queryFn: () => api<RunGraph>(`/runs/${runId}/graph`),
    refetchInterval: 3000,
  });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { nodes, edges, hidden } = useMemo(() => {
    const g = graph.data;
    if (!g) return { nodes: [] as Node[], edges: [] as Edge[], hidden: 0 };
    const children = new Map<string | null, string[]>();
    for (const n of g.nodes) children.set(n.parent_span_id, [...(children.get(n.parent_span_id) ?? []), n.span_id]);
    const byId = new Map(g.nodes.map((n) => [n.span_id, n]));
    const out: Node[] = [];
    const links: Edge[] = [];
    let hiddenCount = 0;
    const row = new Map<number, number>();
    const walk = (id: string, depth: number) => {
      const n = byId.get(id);
      if (!n) return;
      const y = row.get(depth) ?? 0;
      row.set(depth, y + 1);
      const kids = children.get(id) ?? [];
      const folded = kids.length > MAX_VISIBLE && !expanded.has(id);
      out.push({
        id, position: { x: depth * 260, y: y * 80 },
        data: { label: `${n.label} · ${n.actor_id}${folded ? ` (+${kids.length - MAX_VISIBLE} folded)` : ""}` },
        style: { border: `2px solid ${COLORS[n.status] ?? "#888"}`, borderRadius: 8, fontSize: 12, width: 230 },
      });
      const shown = folded ? kids.slice(0, MAX_VISIBLE) : kids;
      hiddenCount += kids.length - shown.length;
      for (const k of shown) {
        links.push({ id: `${id}-${k}`, source: id, target: k, animated: byId.get(k)?.status === "started" });
        walk(k, depth + 1);
      }
    };
    for (const root of children.get(null) ?? []) walk(root, 0);
    return { nodes: out, edges: links, hidden: hiddenCount };
  }, [graph.data, expanded]);

  return (
    <Panel title="Live Execution Graph" actions={<RunPicker value={runId} onChange={onRun} />}>
      <ErrorNote error={graph.error} />
      {!runId ? (
        <Empty>Pick a run to see its span graph.</Empty>
      ) : nodes.length === 0 ? (
        <Empty>No spans yet.</Empty>
      ) : (
        <>
          {(graph.data?.truncated || hidden > 0) && (
            <p className="note">
              {graph.data?.truncated ? "The API truncated this very large graph. " : ""}
              {hidden > 0 && (
                <button onClick={() => setExpanded(new Set(graph.data?.nodes.map((n) => n.span_id)))}>
                  expand {hidden} folded nodes
                </button>
              )}
            </p>
          )}
          <div className="flow">
            <ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.1} nodesDraggable={false}>
              <Background />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </>
      )}
    </Panel>
  );
}
