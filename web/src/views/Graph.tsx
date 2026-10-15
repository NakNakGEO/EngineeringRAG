import { Background, Controls, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, type RunGraph } from "../api";
import { Button } from "../components/ui/button";
import { useTheme } from "../theme";
import { Empty, ErrorNote, Panel } from "../ui";
import { RunPicker, useNewestRun } from "./Mission";
import { PageHelp } from "./PageHelp";

const COLORS: Record<string, string> = { completed: "#16a34a", failed: "#dc2626", denied: "#ea580c", started: "#2563eb" };

/** Spans laid out by depth. Large runs are collapsed: children beyond MAX_VISIBLE stay folded
 * until the user expands that span (incremental graph expansion; the API caps the graph too). */
const MAX_VISIBLE = 60;

export function Graph({ runId, onRun }: { runId: string | null; onRun: (id: string) => void }) {
  const theme = useTheme();
  useNewestRun(runId, onRun);
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
        id, position: { x: depth * 310, y: y * 72 }, sourcePosition: Position.Right, targetPosition: Position.Left,
        data: { label: `${n.label} · ${n.actor_id}${folded ? ` (+${kids.length - MAX_VISIBLE} folded)` : ""}` },
        style: {
          width: 250, fontSize: 12.5, fontWeight: 500, textAlign: "left", padding: "8px 12px", borderRadius: 8,
          background: "var(--card)", color: "var(--foreground)", border: "1px solid var(--border)",
          borderLeft: `4px solid ${COLORS[n.status] ?? "#94a3b8"}`,
        },
      });
      const shown = folded ? kids.slice(0, MAX_VISIBLE) : kids;
      hiddenCount += kids.length - shown.length;
      for (const k of shown) {
        links.push({ id: `${id}-${k}`, source: id, target: k, type: "smoothstep", animated: byId.get(k)?.status === "started" });
        walk(k, depth + 1);
      }
    };
    for (const root of children.get(null) ?? []) walk(root, 0);
    return { nodes: out, edges: links, hidden: hiddenCount };
  }, [graph.data, expanded]);

  return (
    <div className="space-y-5">
    <PageHelp id="run-graph" title="What is the Run Graph?">
      <p>The same run as in Mission Control, drawn as a <b>tree of steps</b>: which step started which, in order. Green finished, red failed, orange was refused by policy, blue is still going.</p>
      <p><b>Why it is here:</b> when something is slow or went wrong, the tree shows where. It is about what the system <i>did</i>; to see how a project&apos;s files and notes connect, use <b>Project Map</b> instead.</p>
    </PageHelp>
    <Panel title="Run Graph" description="The steps of one run. For a map of a project and its notes, use Project Map." actions={<RunPicker value={runId} onChange={onRun} />}>
      <ErrorNote error={graph.error} />
      {!runId ? (
        <Empty>No runs yet.</Empty>
      ) : nodes.length === 0 ? (
        <Empty>No steps recorded yet.</Empty>
      ) : (
        <>
          {(graph.data?.truncated || hidden > 0) && (
            <p className="flex flex-wrap items-center gap-2 text-[13px] text-warning">
              {graph.data?.truncated ? "The API truncated this very large graph. " : ""}
              {hidden > 0 && <Button size="sm" variant="outline" onClick={() => setExpanded(new Set(graph.data?.nodes.map((n) => n.span_id)))}>Expand {hidden} folded nodes</Button>}
            </p>
          )}
          <div className="h-[min(70dvh,720px)] overflow-hidden rounded-lg border">
            <ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.1} nodesDraggable={false} nodesConnectable={false} colorMode={theme}>
              <Background gap={22} size={1.2} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </>
      )}
    </Panel>
    </div>
  );
}
